#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/init.h>
#include <linux/kprobes.h>
#include <linux/sched.h>
#include <linux/netlink.h>
#include <linux/skbuff.h>
#include <linux/string.h>
#include <net/sock.h>

#define NETLINK_AI_AGENT 31
#define COMM_LEN 16

MODULE_LICENSE("GPL");
MODULE_AUTHOR("AI-Agent OS Team");
MODULE_DESCRIPTION("LKM Process Creation Hook for AI Agent OS");
MODULE_VERSION("0.1");

struct process_event {
    pid_t parent_pid;
    pid_t child_pid;
    char comm[COMM_LEN];
};

struct clone_data {
    pid_t parent_pid;
    char comm[COMM_LEN];
};

static struct sock *nl_sk = NULL;
static pid_t daemon_pid = 0;
static DEFINE_SPINLOCK(daemon_lock);

static void send_process_event(pid_t parent_pid, pid_t child_pid, const char *comm) {
    struct sk_buff *skb;
    struct nlmsghdr *nlh;
    struct process_event event;
    int res;
    pid_t target_pid;

    spin_lock(&daemon_lock);
    target_pid = daemon_pid;
    spin_unlock(&daemon_lock);

    if (target_pid == 0 || !nl_sk) {
        return;
    }

    event.parent_pid = parent_pid;
    event.child_pid = child_pid;
    strscpy(event.comm, comm, COMM_LEN);

    skb = nlmsg_new(sizeof(struct process_event), GFP_ATOMIC);
    if (!skb) {
        return;
    }

    nlh = nlmsg_put(skb, 0, 0, NLMSG_DONE, sizeof(struct process_event), 0);
    if (!nlh) {
        kfree_skb(skb);
        return;
    }

    memcpy(nlmsg_data(nlh), &event, sizeof(struct process_event));

    res = netlink_unicast(nl_sk, skb, target_pid, MSG_DONTWAIT);
    if (res < 0) {
        // Delivery failed, daemon may have stopped
        spin_lock(&daemon_lock);
        if (daemon_pid == target_pid) {
            daemon_pid = 0;
        }
        spin_unlock(&daemon_lock);
    }
}

static int clone_entry_handler(struct kretprobe_instance *ri, struct pt_regs *regs) {
    struct clone_data *data = (struct clone_data *)ri->data;

    data->parent_pid = current->pid;
    strscpy(data->comm, current->comm, COMM_LEN);
    return 0;
}

static int clone_ret_handler(struct kretprobe_instance *ri, struct pt_regs *regs) {
    long ret = regs_return_value(regs);
    struct clone_data *data = (struct clone_data *)ri->data;

    if (ret > 0) {
        send_process_event(data->parent_pid, (pid_t)ret, data->comm);
    }
    return 0;
}

static struct kretprobe clone_kretprobe = {
    .handler = clone_ret_handler,
    .entry_handler = clone_entry_handler,
    .data_size = sizeof(struct clone_data),
    .maxactive = 64,
    .kp = {
        .symbol_name = "kernel_clone",
    },
};

static void nl_recv_msg(struct sk_buff *skb) {
    struct nlmsghdr *nlh = (struct nlmsghdr *)skb->data;

    if (nlh && nlh->nlmsg_len >= NLMSG_HDRLEN) {
        spin_lock(&daemon_lock);
        daemon_pid = nlh->nlmsg_pid;
        spin_unlock(&daemon_lock);
        printk(KERN_INFO "ai_process_hook: Registered userspace daemon with PID %d\n", daemon_pid);
    }
}

static int __init ai_hook_init(void) {
    int ret;
    struct netlink_kernel_cfg cfg = {
        .input = nl_recv_msg,
    };

    printk(KERN_INFO "ai_process_hook: Initializing LKM process monitor...\n");

    nl_sk = netlink_kernel_create(&init_net, NETLINK_AI_AGENT, &cfg);
    if (!nl_sk) {
        printk(KERN_ALERT "ai_process_hook: Failed to create netlink socket\n");
        return -ENOMEM;
    }

    ret = register_kretprobe(&clone_kretprobe);
    if (ret < 0) {
        printk(KERN_ALERT "ai_process_hook: Failed to register kretprobe on kernel_clone: %d\n", ret);
        netlink_kernel_release(nl_sk);
        nl_sk = NULL;
        return ret;
    }

    printk(KERN_INFO "ai_process_hook: Registered kretprobe on kernel_clone successfully.\n");
    return 0;
}

static void __exit ai_hook_exit(void) {
    unregister_kretprobe(&clone_kretprobe);
    if (nl_sk) {
        netlink_kernel_release(nl_sk);
        nl_sk = NULL;
    }
    printk(KERN_INFO "ai_process_hook: Unloaded cleanly.\n");
}

module_init(ai_hook_init);
module_exit(ai_hook_exit);
