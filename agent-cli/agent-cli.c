/*
 * agent-cli.c — AI-Agent OS: Phase 11 Streaming Intent CLI
 *
 * Phase 11 upgrade: instead of printing a single 2048-byte kernel response
 * and exiting, agent-cli now:
 *   1. Dispatches the intent via syscall(548) — receives "[TASK:<id>] Running..."
 *   2. Parses the task ID and task log file path from the response
 *   3. Polls the JSONL task log produced by task_runner.py
 *   4. Prints each event as it arrives — giving the user a live terminal view
 *   5. Exits when it reads a "done" event (status DONE, FAILED, or CANCELLED)
 *
 * Falls back gracefully: if the daemon is on an older pre-Phase-11 build (no
 * task ID in the response), it prints the raw 2048-byte response as before.
 */

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <unistd.h>
#include <time.h>

/* JSON parser — tiny, single-header style inline helpers */
#include <ctype.h>

#ifndef __NR_agent_query
#define __NR_agent_query 548
#endif

#define MAX_RESP_LEN   2048
#define MAX_LINE_LEN   8192
#define POLL_INTERVAL_MS 300   /* how often to check task log (ms) */
#define MAX_WAIT_S     660     /* 11 min max wait before giving up */

/* ── Colour helpers (ANSI, skipped when not a TTY) ─────────────────────────── */
static int use_colour = 0;
#define COL_RESET  (use_colour ? "\033[0m"     : "")
#define COL_BOLD   (use_colour ? "\033[1m"     : "")
#define COL_CYAN   (use_colour ? "\033[1;36m"  : "")
#define COL_GREEN  (use_colour ? "\033[1;32m"  : "")
#define COL_YELLOW (use_colour ? "\033[1;33m"  : "")
#define COL_RED    (use_colour ? "\033[1;31m"  : "")
#define COL_DIM    (use_colour ? "\033[2m"     : "")

/* ── Tiny inline JSON field extractor ─────────────────────────────────────── */

/*
 * Find the value of a JSON string field named `key` in `line`.
 * Copies into `out` (max `outlen` bytes).  Returns 1 on success.
 *
 * Only handles simple flat string values — sufficient for our known schema.
 */
static int json_get_str(const char *line, const char *key, char *out, size_t outlen)
{
    char search[128];
    snprintf(search, sizeof(search), "\"%s\"", key);
    const char *p = strstr(line, search);
    if (!p) return 0;
    p += strlen(search);
    while (*p == ' ' || *p == ':') p++;
    if (*p != '"') return 0;
    p++; /* skip opening quote */
    size_t i = 0;
    while (*p && *p != '"' && i + 1 < outlen) {
        if (*p == '\\') { p++; if (*p) out[i++] = *p++; }
        else             out[i++] = *p++;
    }
    out[i] = '\0';
    return 1;
}

/* Find integer value for key in JSON line. Returns default_val on failure. */
static long json_get_int(const char *line, const char *key, long default_val)
{
    char search[128];
    snprintf(search, sizeof(search), "\"%s\"", key);
    const char *p = strstr(line, search);
    if (!p) return default_val;
    p += strlen(search);
    while (*p == ' ' || *p == ':') p++;
    if (!isdigit((unsigned char)*p) && *p != '-') return default_val;
    return strtol(p, NULL, 10);
}

/* ── Pretty-print a single JSONL event line ───────────────────────────────── */
static int handle_event(const char *line, long *last_task_idx, long *last_step)
{
    char event[64] = {0};
    if (!json_get_str(line, "event", event, sizeof(event))) return 0;

    if (strcmp(event, "task_start") == 0 && json_get_int(line, "task_idx", -1) < 0) {
        /* Top-level job start (task_idx absent) */
        char intent[512] = {0};
        json_get_str(line, "intent", intent, sizeof(intent));
        printf("\n%s[AGENT]%s %sStarting:%s %s\n", COL_CYAN, COL_RESET, COL_BOLD, COL_RESET, intent);

    } else if (strcmp(event, "plan") == 0) {
        /* Print the plan */
        printf("%s[PLAN]%s ", COL_CYAN, COL_RESET);
        /* Find the tasks array and print each item */
        const char *tasks_start = strstr(line, "\"tasks\"");
        if (tasks_start) {
            const char *arr = strchr(tasks_start, '[');
            if (arr) {
                printf("\n");
                int idx = 1;
                const char *p = arr + 1;
                while (*p && *p != ']') {
                    while (*p == ' ' || *p == ',') p++;
                    if (*p == '"') {
                        p++;
                        char task_buf[512] = {0};
                        size_t ti = 0;
                        while (*p && *p != '"' && ti + 1 < sizeof(task_buf)) {
                            if (*p == '\\') { p++; if (*p) task_buf[ti++] = *p++; }
                            else             task_buf[ti++] = *p++;
                        }
                        if (*p == '"') p++;
                        printf("  %s%d.%s %s\n", COL_DIM, idx++, COL_RESET, task_buf);
                    } else if (*p) p++;
                }
            }
        }

    } else if (strcmp(event, "task_start") == 0) {
        long tidx = json_get_int(line, "task_idx", -1);
        if (tidx != *last_task_idx) {
            *last_task_idx = tidx;
            *last_step     = -1;
            char task[512] = {0};
            json_get_str(line, "task", task, sizeof(task));
            printf("\n%s[TASK %ld]%s %s\n", COL_YELLOW, tidx + 1, COL_RESET, task);
        }

    } else if (strcmp(event, "step_exec") == 0) {
        char cmd[1024] = {0};
        json_get_str(line, "cmd", cmd, sizeof(cmd));
        long step = json_get_int(line, "step", 0);
        printf("  %s→%s %s%s%s\n", COL_DIM, COL_RESET, COL_BOLD, cmd, COL_RESET);
        *last_step = step;

    } else if (strcmp(event, "result") == 0) {
        long rc     = json_get_int(line, "returncode", 0);
        char stdout_val[2048] = {0};
        json_get_str(line, "stdout", stdout_val, sizeof(stdout_val));
        char stderr_val[512] = {0};
        json_get_str(line, "stderr", stderr_val, sizeof(stderr_val));

        if (rc == 0) {
            printf("  %s✓ exit 0%s", COL_GREEN, COL_RESET);
        } else {
            printf("  %s✗ exit %ld%s", COL_RED, rc, COL_RESET);
        }
        if (stdout_val[0]) printf("\n%s", stdout_val);
        if (stderr_val[0]) printf("\n%sstderr:%s %s", COL_RED, COL_RESET, stderr_val);
        printf("\n");

    } else if (strcmp(event, "verify") == 0) {
        char cond[256] = {0};
        json_get_str(line, "condition", cond, sizeof(cond));
        printf("  %s✓ verified:%s %s\n", COL_GREEN, COL_RESET, cond);

    } else if (strcmp(event, "task_finish") == 0) {
        char summary[512] = {0};
        json_get_str(line, "summary", summary, sizeof(summary));
        printf("  %s✓ done:%s %s\n", COL_GREEN, COL_RESET, summary);

    } else if (strcmp(event, "task_fail") == 0) {
        char reason[512] = {0};
        json_get_str(line, "reason", reason, sizeof(reason));
        char msg[512] = {0};
        json_get_str(line, "msg", msg, sizeof(msg));
        printf("  %s✗ failed:%s %s%s\n", COL_RED, COL_RESET,
               msg[0] ? msg : reason, "");

    } else if (strcmp(event, "security_block") == 0) {
        char cmd[512] = {0}, reason[256] = {0};
        json_get_str(line, "cmd", cmd, sizeof(cmd));
        json_get_str(line, "reason", reason, sizeof(reason));
        printf("  %s⚠ BLOCKED:%s %s — %s\n", COL_YELLOW, COL_RESET, cmd, reason);

    } else if (strcmp(event, "suggest_skip") == 0) {
        char task[512] = {0};
        json_get_str(line, "task", task, sizeof(task));
        printf("  %s[SUGGEST]%s Would do: %s\n", COL_CYAN, COL_RESET, task);

    } else if (strcmp(event, "done") == 0) {
        char status[32] = {0};
        json_get_str(line, "status", status, sizeof(status));
        long n     = json_get_int(line, "task_count", 0);
        long elapsed = (long)json_get_int(line, "elapsed_s", 0);

        printf("\n%s══════════════════════════════════════════════%s\n", COL_BOLD, COL_RESET);
        if (strcmp(status, "DONE") == 0) {
            printf("%sRESULT: SUCCESS%s — %ld task(s) completed in %lds\n",
                   COL_GREEN, COL_RESET, n, elapsed);
        } else if (strcmp(status, "FAILED") == 0) {
            printf("%sRESULT: PARTIAL/FAILED%s — %ld task(s), %lds elapsed\n",
                   COL_RED, COL_RESET, n, elapsed);
        } else {
            printf("RESULT: %s\n", status);
        }
        printf("%s══════════════════════════════════════════════%s\n", COL_BOLD, COL_RESET);
        return 1;  /* signal caller: we're done */
    }

    fflush(stdout);
    return 0;
}

/* ── Poll the task log file until "done" or timeout ─────────────────────── */
static void stream_task_log(const char *task_file)
{
    FILE *f = NULL;
    time_t start = time(NULL);
    char line[MAX_LINE_LEN];
    long last_task_idx = -1, last_step = -1;
    int done = 0;

    printf("%s[agent-cli]%s Streaming task log: %s%s%s\n\n",
           COL_DIM, COL_RESET, COL_DIM, task_file, COL_RESET);
    fflush(stdout);

    while (!done) {
        if (difftime(time(NULL), start) > MAX_WAIT_S) {
            printf("%s[agent-cli] Timed out waiting for task to complete.%s\n",
                   COL_RED, COL_RESET);
            break;
        }

        if (!f) {
            f = fopen(task_file, "r");
            if (!f) {
                /* File not created yet — task_runner still starting up */
                struct timespec ts = { 0, POLL_INTERVAL_MS * 1000000L };
                nanosleep(&ts, NULL);
                continue;
            }
        }

        /* Read all new lines */
        int got_any = 0;
        while (fgets(line, sizeof(line), f)) {
            got_any = 1;
            size_t len = strlen(line);
            if (len > 0 && line[len - 1] == '\n') line[len - 1] = '\0';
            if (!*line) continue;
            if (handle_event(line, &last_task_idx, &last_step)) {
                done = 1;
                break;
            }
        }

        if (!done) {
            struct timespec ts = { 0, POLL_INTERVAL_MS * 1000000L };
            nanosleep(&ts, NULL);
            /* Re-open is not needed — we keep fgets cursor position in f */
            (void)got_any;
        }
    }

    if (f) fclose(f);
}

/* ── main ────────────────────────────────────────────────────────────────── */
int main(int argc, char *argv[])
{
    if (argc < 2) {
        printf("Usage: %s \"<your intent or instruction>\"\n", argv[0]);
        printf("  %s \"install curl and test it\"\n", argv[0]);
        printf("  %s \"/suggest upgrade all packages\"\n", argv[0]);
        printf("  %s \"/assist check what is listening on port 80\"\n", argv[0]);
        return 1;
    }

    use_colour = isatty(STDOUT_FILENO);

    const char *query = argv[1];
    char response[MAX_RESP_LEN];
    memset(response, 0, sizeof(response));

    printf("%s[agent-cli]%s Dispatching intent to OS AI Agent...\n", COL_CYAN, COL_RESET);
    printf("%s[agent-cli]%s Intent: %s\"%s\"%s\n\n", COL_CYAN, COL_RESET, COL_BOLD, query, COL_RESET);
    fflush(stdout);

    long ret = syscall(__NR_agent_query, 0, query, strlen(query),
                       response, sizeof(response));
    if (ret < 0) {
        perror("[ERROR] Kernel agent subsystem rejected the query");
        return 1;
    }

    /*
     * Phase 11: check if the daemon responded with a task ID.
     * Format: "[TASK:xxxxxxxx] Running in background.\nLog: /var/ai-agent/tasks/xxxxxxxx.jsonl\n..."
     */
    if (strncmp(response, "[TASK:", 6) == 0) {
        /* Parse task_id */
        char task_id[32]   = {0};
        char task_file[512] = {0};

        /* Extract task_id from "[TASK:abc12345]" */
        const char *id_start = response + 6;
        const char *id_end   = strchr(id_start, ']');
        if (id_end) {
            size_t id_len = (size_t)(id_end - id_start);
            if (id_len < sizeof(task_id))
                strncpy(task_id, id_start, id_len);
        }

        /* Extract log path from "Log: /var/ai-agent/tasks/..." */
        const char *log_prefix = "Log: ";
        const char *log_start  = strstr(response, log_prefix);
        if (log_start) {
            log_start += strlen(log_prefix);
            const char *log_end = strchr(log_start, '\n');
            size_t log_len = log_end
                ? (size_t)(log_end - log_start)
                : strlen(log_start);
            if (log_len < sizeof(task_file))
                strncpy(task_file, log_start, log_len);
        }

        if (task_id[0] && task_file[0]) {
            printf("%s[agent-cli]%s Task ID: %s%s%s\n",
                   COL_CYAN, COL_RESET, COL_BOLD, task_id, COL_RESET);
            stream_task_log(task_file);
            return 0;
        }
    }

    /* Fallback: pre-Phase-11 daemon or error — print raw response */
    printf("══════════════════════════════════════════\n");
    printf("OS Agent Response:\n");
    printf("══════════════════════════════════════════\n");
    printf("%s\n", response);
    printf("══════════════════════════════════════════\n");
    return 0;
}
