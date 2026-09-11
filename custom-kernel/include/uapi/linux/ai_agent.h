#ifndef _UAPI_LINUX_AI_AGENT_H
#define _UAPI_LINUX_AI_AGENT_H

#ifdef __KERNEL__
#include <linux/types.h>
#else
#include <stdint.h>
#include <sys/types.h>
#endif

#ifndef __NR_agent_query
#define __NR_agent_query 548
#endif

#define AI_AGENT_MAX_QUERY_LEN    1024
#define AI_AGENT_MAX_RESP_LEN     2048
#define AI_AGENT_NETLINK_PROTO    31

/* Netlink message types */
#define AI_MSG_REGISTER           0
#define AI_MSG_PROCESS_EVENT      1
#define AI_MSG_SYSCALL_QUERY      2
#define AI_MSG_SYSCALL_RESP       3

/* Query packet sent from kernel to userspace agent daemon */
struct ai_agent_query_msg {
    int query_id;
    pid_t caller_pid;
    pid_t target_pid;
    char comm[16];
    char query[AI_AGENT_MAX_QUERY_LEN];
};

/* Response packet sent from userspace agent daemon back to kernel */
struct ai_agent_resp_msg {
    int query_id;
    int status;
    char response[AI_AGENT_MAX_RESP_LEN];
};

#endif /* _UAPI_LINUX_AI_AGENT_H */
