// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 FreePVR Contributors

/*
 * FreePVR / FreeUSC — Standalone Vulkan Compute Pipeline-Cache & Pipeline-Binary Sweeper
 *
 * Clean-room C11 Vulkan capture harness for differential GPU ISA reverse engineering
 * (PowerVR Volcanic DXT-48-1536 / CXTP-48-1536 MC1 and other Vulkan 1.2+ GPUs).
 *
 * For every `.spv` compute shader in `--spv-dir`:
 *   1. Creates an isolated, empty VkPipelineCache.
 *   2. Compiles a VkComputePipeline using a uniform shared VkPipelineLayout
 *      (Descriptor Set 0 Binding 0: STORAGE_BUFFER + 16-byte Compute push constant).
 *   3. Dumps `vkGetPipelineCacheData` -> `<out-dir>/<stem>.pipeline_cache.bin`.
 *   4. If `VK_KHR_pipeline_binary` is supported (confirmed on Pixel 10 vulkan.powervr.so),
 *      captures per-pipeline binaries via `vkCreatePipelineBinariesKHR` +
 *      `vkGetPipelineBinaryDataKHR` -> `<out-dir>/<stem>.pipeline_binary_<idx>.bin`.
 *   5. If `VK_KHR_pipeline_executable_properties` is supported, dumps executable
 *      statistics and internal representations -> `<out-dir>/<stem>.exec_<e>_ir_<r>.{txt,bin}`.
 *   6. Emits `<out-dir>/capture_manifest.json` summarizing device properties, subgroup
 *      width, extensions, and per-probe compile/dump metrics.
 *
 * Build (ARM64 Linux):
 *   gcc -std=c11 -O2 -Wall -Wextra -Werror -o vk_pipeline_cache_sweeper \
 *       vk_pipeline_cache_sweeper.c -lvulkan
 */

#define _POSIX_C_SOURCE 200809L

#include <dirent.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

#include <vulkan/vulkan.h>

#define MAX_PATH_LEN 1024
#define MAX_PROBES 512

typedef struct {
    char stem[256];
    char spv_path[MAX_PATH_LEN];
} ProbeFile;

static int cmp_probe_file(const void *a, const void *b) {
    const ProbeFile *pa = (const ProbeFile *)a;
    const ProbeFile *pb = (const ProbeFile *)b;
    return strcmp(pa->stem, pb->stem);
}

static bool has_suffix(const char *s, const char *suffix) {
    size_t sl = strlen(s);
    size_t sufl = strlen(suffix);
    if (sl < sufl) {
        return false;
    }
    return strcmp(s + (sl - sufl), suffix) == 0;
}

static uint8_t *read_entire_file(const char *path, size_t *out_size) {
    FILE *fp = fopen(path, "rb");
    if (!fp) {
        return NULL;
    }
    if (fseek(fp, 0, SEEK_END) != 0) {
        fclose(fp);
        return NULL;
    }
    long sz = ftell(fp);
    if (sz <= 0 || (sz % 4) != 0) {
        fclose(fp);
        return NULL;
    }
    rewind(fp);
    uint8_t *buf = (uint8_t *)malloc((size_t)sz);
    if (!buf) {
        fclose(fp);
        return NULL;
    }
    if (fread(buf, 1, (size_t)sz, fp) != (size_t)sz) {
        free(buf);
        fclose(fp);
        return NULL;
    }
    fclose(fp);
    *out_size = (size_t)sz;
    return buf;
}

static bool write_entire_file(const char *path, const void *data, size_t size) {
    FILE *fp = fopen(path, "wb");
    if (!fp) {
        return false;
    }
    if (size > 0 && fwrite(data, 1, size, fp) != size) {
        fclose(fp);
        return false;
    }
    return fclose(fp) == 0;
}

static void json_escape_fputs(FILE *fp, const char *s) {
    fputc('"', fp);
    for (const unsigned char *p = (const unsigned char *)s; *p; ++p) {
        if (*p == '"' || *p == '\\') {
            fputc('\\', fp);
            fputc((int)*p, fp);
        } else if (*p == '\n') {
            fputs("\\n", fp);
        } else if (*p == '\r') {
            fputs("\\r", fp);
        } else if (*p == '\t') {
            fputs("\\t", fp);
        } else if (*p < 0x20) {
            fprintf(fp, "\\u%04x", (unsigned)*p);
        } else {
            fputc((int)*p, fp);
        }
    }
    fputc('"', fp);
}

static void bytes_to_hex(const uint8_t *src, size_t n, char *dst) {
    static const char hex[] = "0123456789abcdef";
    for (size_t i = 0; i < n; ++i) {
        dst[i * 2] = hex[(src[i] >> 4) & 0xF];
        dst[i * 2 + 1] = hex[src[i] & 0xF];
    }
    dst[n * 2] = '\0';
}

static bool device_has_extension(const VkExtensionProperties *exts, uint32_t count, const char *name) {
    for (uint32_t i = 0; i < count; ++i) {
        if (strcmp(exts[i].extensionName, name) == 0) {
            return true;
        }
    }
    return false;
}

static int collect_spv_files(const char *spv_dir, const char *filter, ProbeFile *out, int max_out) {
    DIR *dir = opendir(spv_dir);
    if (!dir) {
        fprintf(stderr, "[err] opendir(%s) failed: %s\n", spv_dir, strerror(errno));
        return -1;
    }
    int n = 0;
    struct dirent *ent;
    while ((ent = readdir(dir)) != NULL) {
        if (!has_suffix(ent->d_name, ".spv")) {
            continue;
        }
        if (filter && filter[0] != '\0' && strstr(ent->d_name, filter) == NULL) {
            continue;
        }
        if (n >= max_out) {
            fprintf(stderr, "[warn] Hit MAX_PROBES (%d); truncating list\n", max_out);
            break;
        }
        size_t stem_len = strlen(ent->d_name) - 4;
        if (stem_len >= sizeof(out[n].stem)) {
            stem_len = sizeof(out[n].stem) - 1;
        }
        memcpy(out[n].stem, ent->d_name, stem_len);
        out[n].stem[stem_len] = '\0';
        int written = snprintf(out[n].spv_path, sizeof(out[n].spv_path), "%s/%s", spv_dir, ent->d_name);
        if (written < 0 || (size_t)written >= sizeof(out[n].spv_path)) {
            continue;
        }
        ++n;
    }
    closedir(dir);
    qsort(out, (size_t)n, sizeof(ProbeFile), cmp_probe_file);
    return n;
}

static void usage(const char *prog) {
    fprintf(stderr,
            "FreePVR / FreeUSC — Vulkan Pipeline-Cache & Pipeline-Binary Sweeper\n"
            "Usage: %s --spv-dir <dir> --out-dir <dir> [--device-index <N>] [--filter <substr>]\n",
            prog);
}

int main(int argc, char **argv) {
    const char *spv_dir = NULL;
    const char *out_dir = NULL;
    const char *filter = NULL;
    uint32_t device_index = 0;

    for (int i = 1; i < argc; ++i) {
        if (strcmp(argv[i], "--spv-dir") == 0 && i + 1 < argc) {
            spv_dir = argv[++i];
        } else if (strcmp(argv[i], "--out-dir") == 0 && i + 1 < argc) {
            out_dir = argv[++i];
        } else if (strcmp(argv[i], "--device-index") == 0 && i + 1 < argc) {
            device_index = (uint32_t)strtoul(argv[++i], NULL, 10);
        } else if (strcmp(argv[i], "--filter") == 0 && i + 1 < argc) {
            filter = argv[++i];
        } else if (strcmp(argv[i], "-h") == 0 || strcmp(argv[i], "--help") == 0) {
            usage(argv[0]);
            return 0;
        } else {
            fprintf(stderr, "[err] Unknown option: %s\n", argv[i]);
            usage(argv[0]);
            return 2;
        }
    }

    if (!spv_dir || !out_dir) {
        usage(argv[0]);
        return 2;
    }

    if (mkdir(out_dir, 0755) != 0 && errno != EEXIST) {
        fprintf(stderr, "[err] mkdir(%s) failed: %s\n", out_dir, strerror(errno));
        return 1;
    }

    ProbeFile *probes = (ProbeFile *)calloc(MAX_PROBES, sizeof(ProbeFile));
    if (!probes) {
        fprintf(stderr, "[err] OOM\n");
        return 1;
    }
    int probe_count = collect_spv_files(spv_dir, filter, probes, MAX_PROBES);
    if (probe_count <= 0) {
        fprintf(stderr, "[err] No .spv files found in %s\n", spv_dir);
        free(probes);
        return 1;
    }
    fprintf(stderr, "[info] Found %d .spv probes in %s\n", probe_count, spv_dir);

    /* 1. Create Vulkan 1.2 Instance */
    VkApplicationInfo app_info;
    memset(&app_info, 0, sizeof(app_info));
    app_info.sType = VK_STRUCTURE_TYPE_APPLICATION_INFO;
    app_info.pApplicationName = "FreePVR_PipelineCacheSweeper";
    app_info.applicationVersion = VK_MAKE_VERSION(1, 0, 0);
    app_info.pEngineName = "FreeUSC";
    app_info.engineVersion = VK_MAKE_VERSION(1, 0, 0);
    app_info.apiVersion = VK_API_VERSION_1_2;

    VkInstanceCreateInfo inst_ci;
    memset(&inst_ci, 0, sizeof(inst_ci));
    inst_ci.sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO;
    inst_ci.pApplicationInfo = &app_info;

    VkInstance instance = VK_NULL_HANDLE;
    VkResult vr = vkCreateInstance(&inst_ci, NULL, &instance);
    if (vr != VK_SUCCESS) {
        fprintf(stderr, "[err] vkCreateInstance failed: %d\n", (int)vr);
        free(probes);
        return 1;
    }

    /* 2. Select Physical Device */
    uint32_t phys_count = 0;
    vr = vkEnumeratePhysicalDevices(instance, &phys_count, NULL);
    if (vr != VK_SUCCESS || phys_count == 0) {
        fprintf(stderr, "[err] No Vulkan physical devices found (vr=%d)\n", (int)vr);
        vkDestroyInstance(instance, NULL);
        free(probes);
        return 1;
    }
    VkPhysicalDevice *phys_devs = (VkPhysicalDevice *)calloc(phys_count, sizeof(VkPhysicalDevice));
    vkEnumeratePhysicalDevices(instance, &phys_count, phys_devs);
    if (device_index >= phys_count) {
        fprintf(stderr, "[err] device-index %u out of range (count=%u)\n", device_index, phys_count);
        free(phys_devs);
        vkDestroyInstance(instance, NULL);
        free(probes);
        return 1;
    }
    VkPhysicalDevice phys = phys_devs[device_index];
    free(phys_devs);

    /* Query properties + subgroup properties + driver properties */
    VkPhysicalDeviceSubgroupProperties subgroup_props;
    memset(&subgroup_props, 0, sizeof(subgroup_props));
    subgroup_props.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_SUBGROUP_PROPERTIES;

    VkPhysicalDeviceDriverProperties driver_props;
    memset(&driver_props, 0, sizeof(driver_props));
    driver_props.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_DRIVER_PROPERTIES;
    driver_props.pNext = &subgroup_props;

    VkPhysicalDeviceProperties2 props2;
    memset(&props2, 0, sizeof(props2));
    props2.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2;
    props2.pNext = &driver_props;
    vkGetPhysicalDeviceProperties2(phys, &props2);

    const VkPhysicalDeviceProperties *props = &props2.properties;
    char uuid_hex[VK_UUID_SIZE * 2 + 1];
    bytes_to_hex(props->pipelineCacheUUID, VK_UUID_SIZE, uuid_hex);

    fprintf(stderr,
            "[info] GPU[%u]: %s (vendor=0x%04x device=0x%04x api=%u.%u.%u driver=0x%x subgroupSize=%u)\n",
            device_index,
            props->deviceName,
            props->vendorID,
            props->deviceID,
            VK_API_VERSION_MAJOR(props->apiVersion),
            VK_API_VERSION_MINOR(props->apiVersion),
            VK_API_VERSION_PATCH(props->apiVersion),
            props->driverVersion,
            subgroup_props.subgroupSize);

    /* Query queue families for compute */
    uint32_t qf_count = 0;
    vkGetPhysicalDeviceQueueFamilyProperties(phys, &qf_count, NULL);
    VkQueueFamilyProperties *qf_props = (VkQueueFamilyProperties *)calloc(qf_count, sizeof(VkQueueFamilyProperties));
    vkGetPhysicalDeviceQueueFamilyProperties(phys, &qf_count, qf_props);
    uint32_t compute_qf = UINT32_MAX;
    for (uint32_t i = 0; i < qf_count; ++i) {
        if (qf_props[i].queueFlags & VK_QUEUE_COMPUTE_BIT) {
            compute_qf = i;
            break;
        }
    }
    free(qf_props);
    if (compute_qf == UINT32_MAX) {
        fprintf(stderr, "[err] No compute queue family found\n");
        vkDestroyInstance(instance, NULL);
        free(probes);
        return 1;
    }

    /* Enumerate device extensions */
    uint32_t ext_count = 0;
    vkEnumerateDeviceExtensionProperties(phys, NULL, &ext_count, NULL);
    VkExtensionProperties *exts = (VkExtensionProperties *)calloc(ext_count > 0 ? ext_count : 1, sizeof(VkExtensionProperties));
    if (ext_count > 0) {
        vkEnumerateDeviceExtensionProperties(phys, NULL, &ext_count, exts);
    }

    bool has_exec_props = false;
    bool has_pipe_binary = false;
    bool has_maint5 = false;

#ifdef VK_KHR_PIPELINE_EXECUTABLE_PROPERTIES_EXTENSION_NAME
    has_exec_props = device_has_extension(exts, ext_count, VK_KHR_PIPELINE_EXECUTABLE_PROPERTIES_EXTENSION_NAME);
#endif
#ifdef VK_KHR_PIPELINE_BINARY_EXTENSION_NAME
    has_pipe_binary = device_has_extension(exts, ext_count, VK_KHR_PIPELINE_BINARY_EXTENSION_NAME);
#endif
#ifdef VK_KHR_MAINTENANCE_5_EXTENSION_NAME
    has_maint5 = device_has_extension(exts, ext_count, VK_KHR_MAINTENANCE_5_EXTENSION_NAME);
#endif
    free(exts);

    /* Query supported features (Vulkan 1.0 shaderInt64 + Vulkan 1.2 BDA + optional extensions) */
    VkPhysicalDeviceVulkan12Features avail_vk12;
    memset(&avail_vk12, 0, sizeof(avail_vk12));
    avail_vk12.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_2_FEATURES;

    void *feat_chain = &avail_vk12;

#ifdef VK_KHR_PIPELINE_EXECUTABLE_PROPERTIES_EXTENSION_NAME
    VkPhysicalDevicePipelineExecutablePropertiesFeaturesKHR avail_exec;
    memset(&avail_exec, 0, sizeof(avail_exec));
    avail_exec.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PIPELINE_EXECUTABLE_PROPERTIES_FEATURES_KHR;
    if (has_exec_props) {
        avail_exec.pNext = feat_chain;
        feat_chain = &avail_exec;
    }
#endif

#ifdef VK_KHR_PIPELINE_BINARY_EXTENSION_NAME
    VkPhysicalDevicePipelineBinaryFeaturesKHR avail_pbin;
    memset(&avail_pbin, 0, sizeof(avail_pbin));
    avail_pbin.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PIPELINE_BINARY_FEATURES_KHR;
    if (has_pipe_binary) {
        avail_pbin.pNext = feat_chain;
        feat_chain = &avail_pbin;
    }
#endif

    VkPhysicalDeviceFeatures2 avail_feats2;
    memset(&avail_feats2, 0, sizeof(avail_feats2));
    avail_feats2.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_FEATURES_2;
    avail_feats2.pNext = feat_chain;
    vkGetPhysicalDeviceFeatures2(phys, &avail_feats2);

    /* Build enabled extension list and feature chain */
    const char *enabled_exts[8];
    uint32_t enabled_ext_count = 0;

    VkPhysicalDeviceVulkan12Features enable_vk12;
    memset(&enable_vk12, 0, sizeof(enable_vk12));
    enable_vk12.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_2_FEATURES;
    enable_vk12.bufferDeviceAddress = avail_vk12.bufferDeviceAddress;

    void *dev_pnext = &enable_vk12;

#ifdef VK_KHR_PIPELINE_EXECUTABLE_PROPERTIES_EXTENSION_NAME
    VkPhysicalDevicePipelineExecutablePropertiesFeaturesKHR enable_exec;
    memset(&enable_exec, 0, sizeof(enable_exec));
    enable_exec.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PIPELINE_EXECUTABLE_PROPERTIES_FEATURES_KHR;
    if (has_exec_props && avail_exec.pipelineExecutableInfo) {
        enable_exec.pipelineExecutableInfo = VK_TRUE;
        enable_exec.pNext = dev_pnext;
        dev_pnext = &enable_exec;
        enabled_exts[enabled_ext_count++] = VK_KHR_PIPELINE_EXECUTABLE_PROPERTIES_EXTENSION_NAME;
    } else {
        has_exec_props = false;
    }
#endif

#ifdef VK_KHR_PIPELINE_BINARY_EXTENSION_NAME
    VkPhysicalDevicePipelineBinaryFeaturesKHR enable_pbin;
    memset(&enable_pbin, 0, sizeof(enable_pbin));
    enable_pbin.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PIPELINE_BINARY_FEATURES_KHR;
    if (has_pipe_binary && avail_pbin.pipelineBinaries) {
        enable_pbin.pipelineBinaries = VK_TRUE;
        enable_pbin.pNext = dev_pnext;
        dev_pnext = &enable_pbin;
        enabled_exts[enabled_ext_count++] = VK_KHR_PIPELINE_BINARY_EXTENSION_NAME;
#ifdef VK_KHR_MAINTENANCE_5_EXTENSION_NAME
        if (has_maint5) {
            enabled_exts[enabled_ext_count++] = VK_KHR_MAINTENANCE_5_EXTENSION_NAME;
        }
#endif
    } else {
        has_pipe_binary = false;
    }
#endif

    VkPhysicalDeviceFeatures enable_feats;
    memset(&enable_feats, 0, sizeof(enable_feats));
    enable_feats.shaderInt64 = avail_feats2.features.shaderInt64;

    float queue_prio = 1.0f;
    VkDeviceQueueCreateInfo qci;
    memset(&qci, 0, sizeof(qci));
    qci.sType = VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO;
    qci.queueFamilyIndex = compute_qf;
    qci.queueCount = 1;
    qci.pQueuePriorities = &queue_prio;

    VkDeviceCreateInfo dci;
    memset(&dci, 0, sizeof(dci));
    dci.sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO;
    dci.pNext = dev_pnext;
    dci.queueCreateInfoCount = 1;
    dci.pQueueCreateInfos = &qci;
    dci.enabledExtensionCount = enabled_ext_count;
    dci.ppEnabledExtensionNames = enabled_exts;
    dci.pEnabledFeatures = &enable_feats;

    VkDevice device = VK_NULL_HANDLE;
    vr = vkCreateDevice(phys, &dci, NULL, &device);
    if (vr != VK_SUCCESS) {
        fprintf(stderr, "[err] vkCreateDevice failed: %d\n", (int)vr);
        vkDestroyInstance(instance, NULL);
        free(probes);
        return 1;
    }

    fprintf(stderr,
            "[info] Enabled features: shaderInt64=%u BDA=%u VK_KHR_pipeline_binary=%d VK_KHR_pipeline_executable_properties=%d\n",
            enable_feats.shaderInt64,
            enable_vk12.bufferDeviceAddress,
            (int)has_pipe_binary,
            (int)has_exec_props);

    /* Load optional extension function pointers */
#ifdef VK_KHR_PIPELINE_BINARY_EXTENSION_NAME
    PFN_vkCreatePipelineBinariesKHR pfn_create_pbins = NULL;
    PFN_vkGetPipelineBinaryDataKHR pfn_get_pbin_data = NULL;
    PFN_vkDestroyPipelineBinaryKHR pfn_destroy_pbin = NULL;
    PFN_vkReleaseCapturedPipelineDataKHR pfn_release_pbin = NULL;
    if (has_pipe_binary) {
        pfn_create_pbins = (PFN_vkCreatePipelineBinariesKHR)vkGetDeviceProcAddr(device, "vkCreatePipelineBinariesKHR");
        pfn_get_pbin_data = (PFN_vkGetPipelineBinaryDataKHR)vkGetDeviceProcAddr(device, "vkGetPipelineBinaryDataKHR");
        pfn_destroy_pbin = (PFN_vkDestroyPipelineBinaryKHR)vkGetDeviceProcAddr(device, "vkDestroyPipelineBinaryKHR");
        pfn_release_pbin = (PFN_vkReleaseCapturedPipelineDataKHR)vkGetDeviceProcAddr(device, "vkReleaseCapturedPipelineDataKHR");
        if (!pfn_create_pbins || !pfn_get_pbin_data || !pfn_destroy_pbin) {
            fprintf(stderr, "[warn] VK_KHR_pipeline_binary entry points missing; disabling\n");
            has_pipe_binary = false;
        }
    }
#endif

#ifdef VK_KHR_PIPELINE_EXECUTABLE_PROPERTIES_EXTENSION_NAME
    PFN_vkGetPipelineExecutablePropertiesKHR pfn_get_exec_props = NULL;
    PFN_vkGetPipelineExecutableStatisticsKHR pfn_get_exec_stats = NULL;
    PFN_vkGetPipelineExecutableInternalRepresentationsKHR pfn_get_exec_irs = NULL;
    if (has_exec_props) {
        pfn_get_exec_props = (PFN_vkGetPipelineExecutablePropertiesKHR)vkGetDeviceProcAddr(
            device, "vkGetPipelineExecutablePropertiesKHR");
        pfn_get_exec_stats = (PFN_vkGetPipelineExecutableStatisticsKHR)vkGetDeviceProcAddr(
            device, "vkGetPipelineExecutableStatisticsKHR");
        pfn_get_exec_irs = (PFN_vkGetPipelineExecutableInternalRepresentationsKHR)vkGetDeviceProcAddr(
            device, "vkGetPipelineExecutableInternalRepresentationsKHR");
        if (!pfn_get_exec_props || !pfn_get_exec_irs) {
            fprintf(stderr, "[warn] VK_KHR_pipeline_executable_properties entry points missing; disabling\n");
            has_exec_props = false;
        }
    }
#endif

    /* 3. Create uniform shared DescriptorSetLayout & PipelineLayout */
    VkDescriptorSetLayoutBinding ssbo_binding;
    memset(&ssbo_binding, 0, sizeof(ssbo_binding));
    ssbo_binding.binding = 0;
    ssbo_binding.descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
    ssbo_binding.descriptorCount = 1;
    ssbo_binding.stageFlags = VK_SHADER_STAGE_COMPUTE_BIT;

    VkDescriptorSetLayoutCreateInfo dsl_ci;
    memset(&dsl_ci, 0, sizeof(dsl_ci));
    dsl_ci.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO;
    dsl_ci.bindingCount = 1;
    dsl_ci.pBindings = &ssbo_binding;

    VkDescriptorSetLayout dsl = VK_NULL_HANDLE;
    vr = vkCreateDescriptorSetLayout(device, &dsl_ci, NULL, &dsl);
    if (vr != VK_SUCCESS) {
        fprintf(stderr, "[err] vkCreateDescriptorSetLayout failed: %d\n", (int)vr);
        vkDestroyDevice(device, NULL);
        vkDestroyInstance(instance, NULL);
        free(probes);
        return 1;
    }

    VkPushConstantRange pc_range;
    memset(&pc_range, 0, sizeof(pc_range));
    pc_range.stageFlags = VK_SHADER_STAGE_COMPUTE_BIT;
    pc_range.offset = 0;
    pc_range.size = 16; /* two uint64_t BDA pointers */

    VkPipelineLayoutCreateInfo pl_ci;
    memset(&pl_ci, 0, sizeof(pl_ci));
    pl_ci.sType = VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO;
    pl_ci.setLayoutCount = 1;
    pl_ci.pSetLayouts = &dsl;
    pl_ci.pushConstantRangeCount = 1;
    pl_ci.pPushConstantRanges = &pc_range;

    VkPipelineLayout pipe_layout = VK_NULL_HANDLE;
    vr = vkCreatePipelineLayout(device, &pl_ci, NULL, &pipe_layout);
    if (vr != VK_SUCCESS) {
        fprintf(stderr, "[err] vkCreatePipelineLayout failed: %d\n", (int)vr);
        vkDestroyDescriptorSetLayout(device, dsl, NULL);
        vkDestroyDevice(device, NULL);
        vkDestroyInstance(instance, NULL);
        free(probes);
        return 1;
    }

    /* 4. Open capture_manifest.json */
    char manifest_path[MAX_PATH_LEN];
    snprintf(manifest_path, sizeof(manifest_path), "%s/capture_manifest.json", out_dir);
    FILE *mfp = fopen(manifest_path, "wb");
    if (!mfp) {
        fprintf(stderr, "[err] Cannot create %s: %s\n", manifest_path, strerror(errno));
        vkDestroyPipelineLayout(device, pipe_layout, NULL);
        vkDestroyDescriptorSetLayout(device, dsl, NULL);
        vkDestroyDevice(device, NULL);
        vkDestroyInstance(instance, NULL);
        free(probes);
        return 1;
    }

    fprintf(mfp, "{\n");
    fprintf(mfp, "  \"schema_version\": 1,\n");
    fprintf(mfp, "  \"device\": {\n");
    fprintf(mfp, "    \"device_name\": ");
    json_escape_fputs(mfp, props->deviceName);
    fprintf(mfp, ",\n");
    fprintf(mfp, "    \"vendor_id\": \"0x%04x\",\n", props->vendorID);
    fprintf(mfp, "    \"device_id\": \"0x%04x\",\n", props->deviceID);
    fprintf(mfp, "    \"api_version\": \"%u.%u.%u\",\n",
            VK_API_VERSION_MAJOR(props->apiVersion),
            VK_API_VERSION_MINOR(props->apiVersion),
            VK_API_VERSION_PATCH(props->apiVersion));
    fprintf(mfp, "    \"driver_version\": \"0x%08x\",\n", props->driverVersion);
    fprintf(mfp, "    \"driver_name\": ");
    json_escape_fputs(mfp, driver_props.driverName);
    fprintf(mfp, ",\n");
    fprintf(mfp, "    \"driver_info\": ");
    json_escape_fputs(mfp, driver_props.driverInfo);
    fprintf(mfp, ",\n");
    fprintf(mfp, "    \"pipeline_cache_uuid\": \"%s\",\n", uuid_hex);
    fprintf(mfp, "    \"subgroup_size\": %u,\n", subgroup_props.subgroupSize);
    fprintf(mfp, "    \"shader_int64\": %s,\n", enable_feats.shaderInt64 ? "true" : "false");
    fprintf(mfp, "    \"buffer_device_address\": %s,\n", enable_vk12.bufferDeviceAddress ? "true" : "false");
    fprintf(mfp, "    \"has_pipeline_binary\": %s,\n", has_pipe_binary ? "true" : "false");
    fprintf(mfp, "    \"has_pipeline_executable_properties\": %s\n", has_exec_props ? "true" : "false");
    fprintf(mfp, "  },\n");
    fprintf(mfp, "  \"probes\": [\n");

    int ok_count = 0;
    for (int i = 0; i < probe_count; ++i) {
        size_t spv_size = 0;
        uint8_t *spv_bytes = read_entire_file(probes[i].spv_path, &spv_size);

        fprintf(mfp, "    {\n");
        fprintf(mfp, "      \"name\": ");
        json_escape_fputs(mfp, probes[i].stem);
        fprintf(mfp, ",\n");
        fprintf(mfp, "      \"spv_size\": %zu,\n", spv_size);

        if (!spv_bytes) {
            fprintf(stderr, "[err] Failed reading %s\n", probes[i].spv_path);
            fprintf(mfp, "      \"status\": \"READ_ERROR\"\n");
            fprintf(mfp, "    }%s\n", (i + 1 < probe_count) ? "," : "");
            continue;
        }

        VkShaderModuleCreateInfo sm_ci;
        memset(&sm_ci, 0, sizeof(sm_ci));
        sm_ci.sType = VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO;
        sm_ci.codeSize = spv_size;
        sm_ci.pCode = (const uint32_t *)spv_bytes;

        VkShaderModule sm = VK_NULL_HANDLE;
        vr = vkCreateShaderModule(device, &sm_ci, NULL, &sm);
        free(spv_bytes);
        if (vr != VK_SUCCESS) {
            fprintf(stderr, "[err] vkCreateShaderModule(%s) failed: %d\n", probes[i].stem, (int)vr);
            fprintf(mfp, "      \"status\": \"SHADER_MODULE_ERROR\",\n");
            fprintf(mfp, "      \"vk_result\": %d\n", (int)vr);
            fprintf(mfp, "    }%s\n", (i + 1 < probe_count) ? "," : "");
            continue;
        }

        /* Fresh isolated VkPipelineCache per probe */
        VkPipelineCacheCreateInfo pc_ci;
        memset(&pc_ci, 0, sizeof(pc_ci));
        pc_ci.sType = VK_STRUCTURE_TYPE_PIPELINE_CACHE_CREATE_INFO;

        VkPipelineCache cache = VK_NULL_HANDLE;
        vr = vkCreatePipelineCache(device, &pc_ci, NULL, &cache);
        if (vr != VK_SUCCESS) {
            vkDestroyShaderModule(device, sm, NULL);
            fprintf(mfp, "      \"status\": \"CACHE_CREATE_ERROR\",\n");
            fprintf(mfp, "      \"vk_result\": %d\n", (int)vr);
            fprintf(mfp, "    }%s\n", (i + 1 < probe_count) ? "," : "");
            continue;
        }

        VkComputePipelineCreateInfo cp_ci;
        memset(&cp_ci, 0, sizeof(cp_ci));
        cp_ci.sType = VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO;
        cp_ci.stage.sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO;
        cp_ci.stage.stage = VK_SHADER_STAGE_COMPUTE_BIT;
        cp_ci.stage.module = sm;
        cp_ci.stage.pName = "main";
        cp_ci.layout = pipe_layout;

        if (has_exec_props) {
#ifdef VK_KHR_PIPELINE_EXECUTABLE_PROPERTIES_EXTENSION_NAME
            cp_ci.flags |= VK_PIPELINE_CREATE_CAPTURE_STATISTICS_BIT_KHR |
                           VK_PIPELINE_CREATE_CAPTURE_INTERNAL_REPRESENTATIONS_BIT_KHR;
#endif
        }

#if defined(VK_KHR_PIPELINE_BINARY_EXTENSION_NAME) && defined(VK_KHR_MAINTENANCE_5_EXTENSION_NAME)
        VkPipelineCreateFlags2CreateInfoKHR flags2_ci;
        memset(&flags2_ci, 0, sizeof(flags2_ci));
        if (has_pipe_binary && has_maint5) {
            flags2_ci.sType = VK_STRUCTURE_TYPE_PIPELINE_CREATE_FLAGS_2_CREATE_INFO_KHR;
            flags2_ci.flags = (VkPipelineCreateFlags2KHR)cp_ci.flags |
                              VK_PIPELINE_CREATE_2_CAPTURE_DATA_BIT_KHR;
            cp_ci.pNext = &flags2_ci;
        }
#endif

        VkPipeline pipe = VK_NULL_HANDLE;
        vr = vkCreateComputePipelines(device, cache, 1, &cp_ci, NULL, &pipe);
        if (vr != VK_SUCCESS) {
            fprintf(stderr, "[warn] vkCreateComputePipelines(%s) failed: %d\n", probes[i].stem, (int)vr);
            vkDestroyPipelineCache(device, cache, NULL);
            vkDestroyShaderModule(device, sm, NULL);
            fprintf(mfp, "      \"status\": \"PIPELINE_CREATE_ERROR\",\n");
            fprintf(mfp, "      \"vk_result\": %d\n", (int)vr);
            fprintf(mfp, "    }%s\n", (i + 1 < probe_count) ? "," : "");
            continue;
        }

        ++ok_count;

        /* Capture A: vkGetPipelineCacheData */
        size_t cache_size = 0;
        char cache_file_rel[320] = "";
        vr = vkGetPipelineCacheData(device, cache, &cache_size, NULL);
        if (vr == VK_SUCCESS && cache_size > 0) {
            uint8_t *cache_buf = (uint8_t *)malloc(cache_size);
            if (cache_buf && vkGetPipelineCacheData(device, cache, &cache_size, cache_buf) == VK_SUCCESS) {
                char out_path[MAX_PATH_LEN];
                snprintf(cache_file_rel, sizeof(cache_file_rel), "%s.pipeline_cache.bin", probes[i].stem);
                snprintf(out_path, sizeof(out_path), "%s/%s", out_dir, cache_file_rel);
                write_entire_file(out_path, cache_buf, cache_size);
            }
            free(cache_buf);
        }

        fprintf(mfp, "      \"status\": \"OK\",\n");
        fprintf(mfp, "      \"pipeline_cache_file\": ");
        json_escape_fputs(mfp, cache_file_rel);
        fprintf(mfp, ",\n");
        fprintf(mfp, "      \"pipeline_cache_size\": %zu,\n", cache_size);

        /* Capture B: VK_KHR_pipeline_binary */
        fprintf(mfp, "      \"pipeline_binaries\": [");
#ifdef VK_KHR_PIPELINE_BINARY_EXTENSION_NAME
        if (has_pipe_binary) {
            VkPipelineBinaryCreateInfoKHR pb_ci;
            memset(&pb_ci, 0, sizeof(pb_ci));
            pb_ci.sType = VK_STRUCTURE_TYPE_PIPELINE_BINARY_CREATE_INFO_KHR;
            pb_ci.pipeline = pipe;

            VkPipelineBinaryHandlesInfoKHR handles;
            memset(&handles, 0, sizeof(handles));
            handles.sType = VK_STRUCTURE_TYPE_PIPELINE_BINARY_HANDLES_INFO_KHR;

            if (pfn_create_pbins(device, &pb_ci, NULL, &handles) == VK_SUCCESS && handles.pipelineBinaryCount > 0) {
                VkPipelineBinaryKHR *bins = (VkPipelineBinaryKHR *)calloc(
                    handles.pipelineBinaryCount, sizeof(VkPipelineBinaryKHR));
                handles.pPipelineBinaries = bins;
                if (bins && pfn_create_pbins(device, &pb_ci, NULL, &handles) == VK_SUCCESS) {
                    for (uint32_t b = 0; b < handles.pipelineBinaryCount; ++b) {
                        VkPipelineBinaryDataInfoKHR dinfo;
                        memset(&dinfo, 0, sizeof(dinfo));
                        dinfo.sType = VK_STRUCTURE_TYPE_PIPELINE_BINARY_DATA_INFO_KHR;
                        dinfo.pipelineBinary = bins[b];

                        VkPipelineBinaryKeyKHR bkey;
                        memset(&bkey, 0, sizeof(bkey));
                        bkey.sType = VK_STRUCTURE_TYPE_PIPELINE_BINARY_KEY_KHR;

                        size_t bsize = 0;
                        char bfile_rel[320] = "";
                        char bkey_hex[VK_MAX_PIPELINE_BINARY_KEY_SIZE_KHR * 2 + 1] = "";

                        if (pfn_get_pbin_data(device, &dinfo, &bkey, &bsize, NULL) == VK_SUCCESS && bsize > 0) {
                            uint8_t *bbuf = (uint8_t *)malloc(bsize);
                            if (bbuf && pfn_get_pbin_data(device, &dinfo, &bkey, &bsize, bbuf) == VK_SUCCESS) {
                                char out_path[MAX_PATH_LEN];
                                snprintf(bfile_rel, sizeof(bfile_rel), "%s.pipeline_binary_%u.bin", probes[i].stem, b);
                                snprintf(out_path, sizeof(out_path), "%s/%s", out_dir, bfile_rel);
                                write_entire_file(out_path, bbuf, bsize);
                                uint32_t klen = bkey.keySize <= VK_MAX_PIPELINE_BINARY_KEY_SIZE_KHR
                                                    ? bkey.keySize
                                                    : VK_MAX_PIPELINE_BINARY_KEY_SIZE_KHR;
                                bytes_to_hex(bkey.key, klen, bkey_hex);
                            }
                            free(bbuf);
                        }
                        fprintf(mfp, "%s{\"file\": ", (b > 0) ? ", " : "");
                        json_escape_fputs(mfp, bfile_rel);
                        fprintf(mfp, ", \"size\": %zu, \"key\": \"%s\"}", bsize, bkey_hex);
                        pfn_destroy_pbin(device, bins[b], NULL);
                    }
                }
                free(bins);
            }
            if (pfn_release_pbin) {
                VkReleaseCapturedPipelineDataInfoKHR rel_info;
                memset(&rel_info, 0, sizeof(rel_info));
                rel_info.sType = VK_STRUCTURE_TYPE_RELEASE_CAPTURED_PIPELINE_DATA_INFO_KHR;
                rel_info.pipeline = pipe;
                pfn_release_pbin(device, &rel_info, NULL);
            }
        }
#endif
        fprintf(mfp, "],\n");

        /* Capture C: VK_KHR_pipeline_executable_properties */
        fprintf(mfp, "      \"executables\": [");
#ifdef VK_KHR_PIPELINE_EXECUTABLE_PROPERTIES_EXTENSION_NAME
        if (has_exec_props) {
            VkPipelineInfoKHR pinfo;
            memset(&pinfo, 0, sizeof(pinfo));
            pinfo.sType = VK_STRUCTURE_TYPE_PIPELINE_INFO_KHR;
            pinfo.pipeline = pipe;

            uint32_t exec_count = 0;
            if (pfn_get_exec_props(device, &pinfo, &exec_count, NULL) == VK_SUCCESS && exec_count > 0) {
                VkPipelineExecutablePropertiesKHR *eprops = (VkPipelineExecutablePropertiesKHR *)calloc(
                    exec_count, sizeof(VkPipelineExecutablePropertiesKHR));
                for (uint32_t e = 0; e < exec_count; ++e) {
                    eprops[e].sType = VK_STRUCTURE_TYPE_PIPELINE_EXECUTABLE_PROPERTIES_KHR;
                }
                if (pfn_get_exec_props(device, &pinfo, &exec_count, eprops) == VK_SUCCESS) {
                    for (uint32_t e = 0; e < exec_count; ++e) {
                        VkPipelineExecutableInfoKHR einfo;
                        memset(&einfo, 0, sizeof(einfo));
                        einfo.sType = VK_STRUCTURE_TYPE_PIPELINE_EXECUTABLE_INFO_KHR;
                        einfo.pipeline = pipe;
                        einfo.executableIndex = e;

                        fprintf(mfp, "%s{\"name\": ", (e > 0) ? ", " : "");
                        json_escape_fputs(mfp, eprops[e].name);
                        fprintf(mfp, ", \"subgroup_size\": %u, \"irs\": [", eprops[e].subgroupSize);

                        uint32_t ir_count = 0;
                        if (pfn_get_exec_irs(device, &einfo, &ir_count, NULL) == VK_SUCCESS && ir_count > 0) {
                            VkPipelineExecutableInternalRepresentationKHR *irs =
                                (VkPipelineExecutableInternalRepresentationKHR *)calloc(
                                    ir_count, sizeof(VkPipelineExecutableInternalRepresentationKHR));
                            for (uint32_t r = 0; r < ir_count; ++r) {
                                irs[r].sType = VK_STRUCTURE_TYPE_PIPELINE_EXECUTABLE_INTERNAL_REPRESENTATION_KHR;
                            }
                            if (pfn_get_exec_irs(device, &einfo, &ir_count, irs) == VK_SUCCESS) {
                                for (uint32_t r = 0; r < ir_count; ++r) {
                                    if (irs[r].dataSize > 0) {
                                        irs[r].pData = malloc(irs[r].dataSize);
                                    }
                                }
                                if (pfn_get_exec_irs(device, &einfo, &ir_count, irs) == VK_SUCCESS) {
                                    for (uint32_t r = 0; r < ir_count; ++r) {
                                        char ir_rel[320] = "";
                                        if (irs[r].pData && irs[r].dataSize > 0) {
                                            char out_path[MAX_PATH_LEN];
                                            snprintf(ir_rel, sizeof(ir_rel), "%s.exec_%u_ir_%u.%s",
                                                     probes[i].stem, e, r, irs[r].isText ? "txt" : "bin");
                                            snprintf(out_path, sizeof(out_path), "%s/%s", out_dir, ir_rel);
                                            write_entire_file(out_path, irs[r].pData, irs[r].dataSize);
                                        }
                                        fprintf(mfp, "%s{\"name\": ", (r > 0) ? ", " : "");
                                        json_escape_fputs(mfp, irs[r].name);
                                        fprintf(mfp, ", \"is_text\": %s, \"size\": %zu, \"file\": ",
                                                irs[r].isText ? "true" : "false", irs[r].dataSize);
                                        json_escape_fputs(mfp, ir_rel);
                                        fprintf(mfp, "}");
                                        free(irs[r].pData);
                                    }
                                } else {
                                    for (uint32_t r = 0; r < ir_count; ++r) {
                                        free(irs[r].pData);
                                    }
                                }
                            }
                            free(irs);
                        }
                        fprintf(mfp, "]}");
                    }
                }
                free(eprops);
            }
            (void)pfn_get_exec_stats;
        }
#endif
        fprintf(mfp, "]\n");
        fprintf(mfp, "    }%s\n", (i + 1 < probe_count) ? "," : "");

        vkDestroyPipeline(device, pipe, NULL);
        vkDestroyPipelineCache(device, cache, NULL);
        vkDestroyShaderModule(device, sm, NULL);
    }

    fprintf(mfp, "  ]\n}\n");
    fclose(mfp);

    fprintf(stderr, "[ok] Compiled & captured %d/%d probes -> %s\n", ok_count, probe_count, manifest_path);

    vkDestroyPipelineLayout(device, pipe_layout, NULL);
    vkDestroyDescriptorSetLayout(device, dsl, NULL);
    vkDestroyDevice(device, NULL);
    vkDestroyInstance(instance, NULL);
    free(probes);
    return (ok_count > 0) ? 0 : 1;
}
