#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/syscall.h>
#include <errno.h>

#ifndef __NR_agent_query
#define __NR_agent_query 548
#endif

#define MAX_RESP_LEN 2048

int main(int argc, char *argv[]) {
    if (argc < 2) {
        printf("Usage: %s \"<your instruction or intent>\"\n", argv[0]);
        printf("Example: %s \"install curl and download the weather for New York\"\n", argv[0]);
        return 1;
    }

    const char *query = argv[1];
    char response[MAX_RESP_LEN];
    long ret;

    printf("[agent-cli] Dispatching intent to OS AI Agent...\n");
    printf("[agent-cli] Intent: \"%s\"\n\n", query);

    memset(response, 0, sizeof(response));

    // target_pid=0 means we are querying on behalf of ourselves
    ret = syscall(__NR_agent_query, 0, query, strlen(query), response, sizeof(response));

    if (ret < 0) {
        perror("[ERROR] Kernel agent subsystem rejected the query");
        return 1;
    }

    printf("==================================================\n");
    printf("OS Agent Response / Execution Result:\n");
    printf("==================================================\n");
    printf("%s\n", response);
    printf("==================================================\n");

    return 0;
}
