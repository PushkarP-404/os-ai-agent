#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/syscall.h>
#include <errno.h>
#include <time.h>

#ifndef __NR_agent_query
#define __NR_agent_query 548
#endif

#define MAX_RESP_LEN 2048

void run_edge_case_tests(void) {
    char resp[MAX_RESP_LEN];
    long ret;

    printf("\n--- Running Kernel Syscall Edge-Case Validation ---\n");

    /* Test 1: NULL query buffer */
    ret = syscall(__NR_agent_query, 0, NULL, 10, resp, sizeof(resp));
    if (ret == -1 && errno == EINVAL) {
        printf("[PASS] NULL query pointer rejected with -EINVAL\n");
    } else {
        printf("[FAIL] NULL query pointer test unexpected ret=%ld, errno=%d\n", ret, errno);
    }

    /* Test 2: Zero length query */
    ret = syscall(__NR_agent_query, 0, "test", 0, resp, sizeof(resp));
    if (ret == -1 && errno == EINVAL) {
        printf("[PASS] Zero-length query rejected with -EINVAL\n");
    } else {
        printf("[FAIL] Zero-length query test unexpected ret=%ld, errno=%d\n", ret, errno);
    }

    /* Test 3: NULL response buffer */
    ret = syscall(__NR_agent_query, 0, "test", 4, NULL, sizeof(resp));
    if (ret == -1 && errno == EINVAL) {
        printf("[PASS] NULL response buffer rejected with -EINVAL\n");
    } else {
        printf("[FAIL] NULL response buffer test unexpected ret=%ld, errno=%d\n", ret, errno);
    }

    /* Test 4: Faulty memory address */
    ret = syscall(__NR_agent_query, 0, (const char *)0x1234, 10, resp, sizeof(resp));
    if (ret == -1 && errno == EFAULT) {
        printf("[PASS] Invalid memory address rejected with -EFAULT\n");
    } else {
        printf("[FAIL] Invalid memory address test unexpected ret=%ld, errno=%d\n", ret, errno);
    }
}

int main(int argc, char *argv[]) {
    char response[MAX_RESP_LEN];
    const char *query = "Checking process safety and resource state.";
    pid_t target_pid = 0;
    struct timespec start, end;
    long ret;
    double elapsed_ms;

    if (argc > 1) {
        query = argv[1];
    }
    if (argc > 2) {
        target_pid = atoi(argv[2]);
    }

    printf("==================================================\n");
    printf("    AI-Agent OS: Syscall Interface (sys_agent_query) \n");
    printf("==================================================\n");
    printf("Invoking syscall #%d...\n", __NR_agent_query);
    printf("Caller PID: %d\n", getpid());
    printf("Target PID: %d\n", target_pid == 0 ? getpid() : target_pid);
    printf("Query: \"%s\"\n\n", query);

    memset(response, 0, sizeof(response));
    clock_gettime(CLOCK_MONOTONIC, &start);

    ret = syscall(__NR_agent_query, target_pid, query, strlen(query), response, sizeof(response));

    clock_gettime(CLOCK_MONOTONIC, &end);
    elapsed_ms = (end.tv_sec - start.tv_sec) * 1000.0 + (end.tv_nsec - start.tv_nsec) / 1000000.0;

    if (ret < 0) {
        perror("[ERROR] sys_agent_query failed");
        printf("Return value: %ld, Errno: %d (%s)\n", ret, errno, strerror(errno));
        run_edge_case_tests();
        return 1;
    }

    printf("[SUCCESS] Syscall completed in %.2f ms\n", elapsed_ms);
    printf("Bytes returned: %ld\n", ret);
    printf("--------------------------------------------------\n");
    printf("Agent Response:\n%s\n", response);
    printf("--------------------------------------------------\n");

    run_edge_case_tests();

    return 0;
}
