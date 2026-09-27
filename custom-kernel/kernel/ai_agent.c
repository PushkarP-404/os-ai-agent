#include <linux/atomic.h>
#include <linux/init.h>
#include <linux/kref.h>
#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/netlink.h>
#include <linux/sched.h>
#include <linux/skbuff.h>
#include <linux/slab.h>
#include <linux/spinlock.h>
#include <linux/string.h>
#include <linux/syscalls.h>
#include <linux/uaccess.h>
#include <linux/wait.h>
#include <net/sock.h>
#include <uapi/linux/ai_agent.h>

static struct sock *nl_sock = NULL;
static pid_t daemon_pid = 0;
static DEFINE_SPINLOCK(daemon_lock);
static atomic_t next_query_id = ATOMIC_INIT(1);

struct ai_query_waiter {
  int query_id;
  char response[AI_AGENT_MAX_RESP_LEN];
  int status;
  bool completed;
  wait_queue_head_t wq;
  struct list_head list;
  struct kref refcnt;
};

static void ai_query_waiter_release(struct kref *ref) {
  struct ai_query_waiter *w = container_of(ref, struct ai_query_waiter, refcnt);
  kfree(w);
}

static LIST_HEAD(waiter_list);
static DEFINE_SPINLOCK(waiter_lock);

static void ai_nl_recv_msg(struct sk_buff *skb) {
  struct nlmsghdr *nlh;

  if (!skb)
    return;

  nlh = (struct nlmsghdr *)skb->data;
  if (!nlh || skb->len < sizeof(*nlh) || skb->len < nlh->nlmsg_len)
    return;

  if (nlh->nlmsg_type == AI_MSG_REGISTER) {
    /* Only allow registration from privileged processes */
    struct user_namespace *ns = sock_net(skb->sk)->user_ns;
    if (!ns_capable(ns, CAP_SYS_ADMIN)) {
      pr_warn("ai_agent: Registration rejected from unprivileged PID %d\n",
              nlh->nlmsg_pid);
      return;
    }

    spin_lock(&daemon_lock);
    daemon_pid = nlh->nlmsg_pid;
    spin_unlock(&daemon_lock);
    pr_info("ai_agent: Userspace agent daemon registered with PID %d\n",
            nlh->nlmsg_pid);
  } else if (nlh->nlmsg_type == AI_MSG_SYSCALL_RESP) {
    struct ai_agent_resp_msg *resp;
    struct ai_query_waiter *w, *tmp;
    struct ai_query_waiter *found = NULL;
    unsigned long flags;

    if (nlmsg_len(nlh) < sizeof(*resp))
      return;

    resp = (struct ai_agent_resp_msg *)nlmsg_data(nlh);

    spin_lock_irqsave(&waiter_lock, flags);
    list_for_each_entry_safe(w, tmp, &waiter_list, list) {
      if (w->query_id == resp->query_id) {
        kref_get(&w->refcnt);
        found = w;
        break;
      }
    }
    spin_unlock_irqrestore(&waiter_lock, flags);

    if (found) {
      size_t len = strnlen(resp->response, sizeof(found->response) - 1);
      memcpy(found->response, resp->response, len);
      found->response[len] = '\0';
      found->status = resp->status;
      smp_wmb(); /* Ensure data is written before completing */
      found->completed = true;
      wake_up_interruptible(&found->wq);
      kref_put(&found->refcnt, ai_query_waiter_release);
    }
  }
}

SYSCALL_DEFINE5(agent_query, pid_t, target_pid, const char __user *, query,
                size_t, query_len, char __user *, response, size_t, resp_len) {
  char *kquery;
  char *fallback_resp;
  pid_t current_daemon;
  int qid;
  struct ai_query_waiter *waiter;
  struct sk_buff *skb;
  struct nlmsghdr *nlh;
  struct ai_agent_query_msg *qmsg;
  unsigned long flags;
  long ret;
  size_t copy_len;

  if (!query || !response)
    return -EINVAL;

  if (!capable(CAP_SYS_ADMIN))
    return -EPERM;

  if (query_len == 0 || query_len > AI_AGENT_MAX_QUERY_LEN)
    return -EINVAL;

  if (resp_len == 0 || resp_len > AI_AGENT_MAX_RESP_LEN)
    return -EINVAL;

  kquery = kmalloc(query_len + 1, GFP_KERNEL);
  if (!kquery)
    return -ENOMEM;

  if (copy_from_user(kquery, query, query_len)) {
    kfree(kquery);
    return -EFAULT;
  }
  kquery[query_len] = '\0';

  if (target_pid == 0)
    target_pid = current->pid;

  pr_info("ai_agent: [PID %d: %s] queried agent for target %d: \"%s\"\n",
          current->pid, current->comm, target_pid, kquery);

  spin_lock(&daemon_lock);
  current_daemon = daemon_pid;
  spin_unlock(&daemon_lock);

  /* If no userspace daemon is registered, provide Kernel Diagnostics Fallback
   */
  if (current_daemon == 0 || !nl_sock) {
    fallback_resp = kmalloc(AI_AGENT_MAX_RESP_LEN, GFP_KERNEL);
    if (!fallback_resp) {
      kfree(kquery);
      return -ENOMEM;
    }

    snprintf(fallback_resp, AI_AGENT_MAX_RESP_LEN,
             "[KERNEL-AI-SUBSYSTEM] Query received for PID %d (%s). Daemon "
             "offline; kernel status: NORMAL.",
             target_pid, current->comm);

    copy_len = strnlen(fallback_resp, AI_AGENT_MAX_RESP_LEN);
    if (copy_len >= resp_len)
      copy_len = resp_len - 1;

    if (copy_to_user(response, fallback_resp, copy_len)) {
      kfree(fallback_resp);
      kfree(kquery);
      return -EFAULT;
    }

    if (put_user('\0', response + copy_len)) {
      kfree(fallback_resp);
      kfree(kquery);
      return -EFAULT;
    }

    kfree(fallback_resp);
    kfree(kquery);
    return (long)copy_len;
  }

  /* Daemon is active: dispatch via Netlink and await response */
  waiter = kzalloc(sizeof(*waiter), GFP_KERNEL);
  if (!waiter) {
    kfree(kquery);
    return -ENOMEM;
  }

  kref_init(&waiter->refcnt);
  qid = atomic_inc_return(&next_query_id);
  waiter->query_id = qid;
  waiter->completed = false;
  init_waitqueue_head(&waiter->wq);

  spin_lock_irqsave(&waiter_lock, flags);
  list_add_tail(&waiter->list, &waiter_list);
  spin_unlock_irqrestore(&waiter_lock, flags);

  skb = nlmsg_new(sizeof(*qmsg), GFP_KERNEL);
  if (!skb) {
    spin_lock_irqsave(&waiter_lock, flags);
    list_del(&waiter->list);
    spin_unlock_irqrestore(&waiter_lock, flags);
    kref_put(&waiter->refcnt, ai_query_waiter_release);
    kfree(kquery);
    return -ENOMEM;
  }

  nlh = nlmsg_put(skb, 0, 0, AI_MSG_SYSCALL_QUERY, sizeof(*qmsg), 0);
  if (!nlh) {
    kfree_skb(skb);
    spin_lock_irqsave(&waiter_lock, flags);
    list_del(&waiter->list);
    spin_unlock_irqrestore(&waiter_lock, flags);
    kref_put(&waiter->refcnt, ai_query_waiter_release);
    kfree(kquery);
    return -EMSGSIZE;
  }

  qmsg = (struct ai_agent_query_msg *)nlmsg_data(nlh);
  qmsg->query_id = qid;
  qmsg->caller_pid = current->pid;
  qmsg->target_pid = target_pid;
  strncpy(qmsg->comm, current->comm, sizeof(qmsg->comm) - 1);
  qmsg->comm[sizeof(qmsg->comm) - 1] = '\0';
  strncpy(qmsg->query, kquery, sizeof(qmsg->query) - 1);
  qmsg->query[sizeof(qmsg->query) - 1] = '\0';

  ret = nlmsg_unicast(nl_sock, skb, current_daemon);
  kfree(kquery);

  if (ret < 0) {
    pr_warn("ai_agent: Failed to unicast query to daemon PID %d (ret=%ld)\n",
            current_daemon, ret);
            
    /* EDGE-002 Fix: Clear stale daemon_pid on unicast failure */
    spin_lock_irqsave(&daemon_lock, flags);
    if (daemon_pid == current_daemon) {
      daemon_pid = 0;
    }
    spin_unlock_irqrestore(&daemon_lock, flags);

    spin_lock_irqsave(&waiter_lock, flags);
    list_del(&waiter->list);
    spin_unlock_irqrestore(&waiter_lock, flags);
    kref_put(&waiter->refcnt, ai_query_waiter_release);
    return -EIO;
  }

  /* Wait for daemon response (timeout: 120 seconds) */
  ret = wait_event_interruptible_timeout(waiter->wq, waiter->completed,
                                         msecs_to_jiffies(120000));

  spin_lock_irqsave(&waiter_lock, flags);
  list_del(&waiter->list);
  spin_unlock_irqrestore(&waiter_lock, flags);

  if (ret == 0) {
    pr_warn("ai_agent: Query %d timed out waiting for daemon response\n", qid);
    kref_put(&waiter->refcnt, ai_query_waiter_release);
    return -ETIMEDOUT;
  } else if (ret < 0) {
    kref_put(&waiter->refcnt, ai_query_waiter_release);
    return -EINTR;
  }

  copy_len = strnlen(waiter->response, sizeof(waiter->response));
  if (copy_len >= resp_len)
    copy_len = resp_len - 1;

  if (copy_to_user(response, waiter->response, copy_len)) {
    kref_put(&waiter->refcnt, ai_query_waiter_release);
    return -EFAULT;
  }

  if (put_user('\0', response + copy_len)) {
    kref_put(&waiter->refcnt, ai_query_waiter_release);
    return -EFAULT;
  }

  kref_put(&waiter->refcnt, ai_query_waiter_release);
  return (long)copy_len;
}

static int __init ai_agent_subsystem_init(void) {
  struct netlink_kernel_cfg cfg = {
      .input = ai_nl_recv_msg,
  };

  nl_sock = netlink_kernel_create(&init_net, AI_AGENT_NETLINK_PROTO, &cfg);
  if (!nl_sock) {
    pr_err("ai_agent: Failed to create Netlink socket (proto %d)\n",
           AI_AGENT_NETLINK_PROTO);
    return -ENOMEM;
  }

  pr_info("ai_agent: Subsystem initialized successfully (syscall 548 "
          "registered, Netlink proto %d)\n",
          AI_AGENT_NETLINK_PROTO);
  return 0;
}

static void __exit ai_agent_subsystem_exit(void) {
  if (nl_sock) {
    netlink_kernel_release(nl_sock);
    nl_sock = NULL;
  }
  pr_info("ai_agent: Subsystem exited\n");
}

late_initcall(ai_agent_subsystem_init);
module_exit(ai_agent_subsystem_exit);
