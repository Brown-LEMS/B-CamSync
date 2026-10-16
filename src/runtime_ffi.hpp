#pragma once
#include <cstdint>

extern "C" {
struct BcamSyncRuntime;
// Callback outputs are allocated and released by the C++ host; inputs are borrowed for the call.
// The host and adapters outlive the runtime. Its owner stops and joins the worker before destroy.
using BcamSyncHostCall = char* (*)(void*, const char*);
using BcamSyncHostRelease = void (*)(void*, char*);
BcamSyncRuntime* bcamsync_runtime_create(const char* request, void* context,
                                       BcamSyncHostCall call, BcamSyncHostRelease release,
                                       char** error);
void bcamsync_runtime_destroy(BcamSyncRuntime* runtime);
// Each result is an owned JSON envelope, released with bcamsync_runtime_release.
char* bcamsync_runtime_call(BcamSyncRuntime* runtime, const char* request);
void bcamsync_runtime_release(char* response);
}
