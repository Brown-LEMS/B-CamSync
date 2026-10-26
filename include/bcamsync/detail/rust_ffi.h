#pragma once
#include <stdint.h>
#ifndef __cplusplus
#include <stdbool.h>
#endif
#ifdef __cplusplus
extern "C" {
#endif
void bcamsync_rust_string_free(char* value);
char* bcamsync_phase_call(void** handle, const char* operation, const char* input);
void bcamsync_phase_free(void* handle);
void* bcamsync_actuator_new(void);
void bcamsync_actuator_free(void* handle);
char* bcamsync_actuator_call(void* handle, const char* operation, const char* input);
typedef struct BcamsyncSubmission {
    int64_t duration_us;
    uint64_t correction_id;
    bool restoration;
    uint64_t round_id;
} BcamsyncSubmission;
int bcamsync_actuator_next(void* handle, int64_t baseline, int64_t mono, BcamsyncSubmission* output);
int bcamsync_actuator_completed(void* handle, BcamsyncSubmission submitted, uint64_t sequence, int64_t timestamp);
int bcamsync_actuator_cancel_active(void* handle);
#ifdef __cplusplus
}
#endif
