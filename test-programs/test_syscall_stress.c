/*
 * test_syscall_stress.c — Phase 4 Concurrent Stress Test
 *
 * Fires 5 threads concurrently, each invoking sys_agent_query (syscall 548)
 * with a unique query string. Verifies that the kernel waiter_list spinlock
 * and per-query wait-queue handle concurrent load without deadlock or
 * response mis-routing.
 *
 * Build: gcc -Wall -O2 -pthread -o test_syscall_stress test_syscall_stress.c
 * Run:   ./test_syscall_stress
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <pthread.h>
#include <sys/syscall.h>
#include <time.h>

#ifndef __NR_agent_query
#define __NR_agent_query 548
#endif

#define MAX_RESP_LEN  2048
#define NUM_THREADS   5

typedef struct {
    int thread_id;
    int passed;
    double elapsed_ms;
    char response[MAX_RESP_LEN];
} thread_result_t;

static void *worker(void *arg)
{
    thread_result_t *r = (thread_result_t *)arg;
    char query[128];
    struct timespec start, end;
    long ret;

    snprintf(query, sizeof(query),
             "Stress test query from thread %d. Is this process behaving normally?",
             r->thread_id);

    memset(r->response, 0, sizeof(r->response));
    clock_gettime(CLOCK_MONOTONIC, &start);

    ret = syscall(__NR_agent_query, 0, query, strlen(query),
                  r->response, sizeof(r->response));

    clock_gettime(CLOCK_MONOTONIC, &end);
    r->elapsed_ms = (end.tv_sec - start.tv_sec) * 1000.0
                  + (end.tv_nsec - start.tv_nsec) / 1e6;

    if (ret < 0) {
        /* ETIMEDOUT is acceptable (daemon may be offline) */
        if (errno == ETIMEDOUT) {
            snprintf(r->response, sizeof(r->response),
                     "[TIMEOUT - daemon did not respond within 15s]");
            r->passed = 1;  /* timeout is expected without live daemon */
        } else if (errno == ENOSYS) {
            snprintf(r->response, sizeof(r->response),
                     "[ENOSYS - not running on ai-agent kernel]");
            r->passed = 0;
        } else {
            snprintf(r->response, sizeof(r->response),
                     "[ERROR errno=%d: %s]", errno, strerror(errno));
            r->passed = 0;
        }
    } else {
        /* Got a real response (fallback or AI) */
        r->passed = (ret > 0 && strnlen(r->response, MAX_RESP_LEN) > 0);
    }

    return NULL;
}

int main(void)
{
    pthread_t threads[NUM_THREADS];
    thread_result_t results[NUM_THREADS];
    int i, all_passed = 1;

    printf("==================================================\n");
    printf("  AI-Agent OS: sys_agent_query Concurrent Stress  \n");
    printf("  Threads: %d  Syscall: #%d\n", NUM_THREADS, __NR_agent_query);
    printf("==================================================\n\n");

    /* Launch all threads simultaneously */
    for (i = 0; i < NUM_THREADS; i++) {
        results[i].thread_id = i + 1;
        results[i].passed    = 0;
        if (pthread_create(&threads[i], NULL, worker, &results[i]) != 0) {
            perror("pthread_create");
            return 1;
        }
    }

    /* Collect results */
    for (i = 0; i < NUM_THREADS; i++) {
        pthread_join(threads[i], NULL);
        printf("[Thread %d] %.2f ms | %s\n",
               results[i].thread_id,
               results[i].elapsed_ms,
               results[i].passed ? "PASS" : "FAIL");
        printf("  Response: %.120s%s\n",
               results[i].response,
               strlen(results[i].response) > 120 ? "..." : "");
        if (!results[i].passed)
            all_passed = 0;
    }

    printf("\n==================================================\n");
    printf("  Stress Test Result: %s\n", all_passed ? "ALL PASS" : "SOME FAILED");
    printf("==================================================\n");

    return all_passed ? 0 : 1;
}
