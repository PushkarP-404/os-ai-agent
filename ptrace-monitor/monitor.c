#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/ptrace.h>
#include <sys/wait.h>
#include <sys/user.h>
#include <sys/reg.h>
#include <signal.h>
#include <errno.h>

#define MAX_SYSCALLS 500

struct syscall_record {
    int syscall_num;
    long args[6];
    long return_value;
};

int syscall_count = 0;
struct syscall_record syscalls[MAX_SYSCALLS];

void print_syscall(int num, long *args) {
    printf("[SYSCALL %d] num=%d, args=[%ld, %ld, %ld, %ld, %ld, %ld]\n",
           syscall_count, num, args[0], args[1], args[2], args[3], args[4], args[5]);
}

int main(int argc, char *argv[]) {
    if (argc < 2) {
        printf("Usage: %s <program> [args...]\n", argv[0]);
        printf("Example: %s /bin/ls -la\n", argv[0]);
        exit(1);
    }

    pid_t child_pid = fork();
    
    if (child_pid == 0) {
        // Child process
        ptrace(PTRACE_TRACEME, 0, NULL, NULL);
        
        // Execute the target program
        execvp(argv[1], &argv[1]);
        perror("execvp failed");
        exit(1);
    } 
    else if (child_pid > 0) {
        // Parent process
        int status;
        int in_syscall = 0;
        struct user_regs_struct regs;

        printf("Tracing PID %d: %s\n", child_pid, argv[1]);
        printf("=====================================\n\n");

        while (1) {
            // Wait for syscall entry or exit
            if (wait4(child_pid, &status, 0, NULL) == -1) {
                break;
            }

            if (WIFEXITED(status)) {
                printf("\n[PROCESS EXITED] Exit code: %d\n", WEXITSTATUS(status));
                break;
            }

            if (WIFSIGNALED(status)) {
                printf("\n[PROCESS KILLED] Signal: %d\n", WTERMSIG(status));
                break;
            }

            // Get registers
            if (ptrace(PTRACE_GETREGS, child_pid, NULL, &regs) == -1) {
                perror("PTRACE_GETREGS");
                break;
            }

            // Toggle syscall entry/exit
            if (!in_syscall) {
                // Syscall entry
                long syscall_num = regs.orig_rax;
                long args[6] = {regs.rdi, regs.rsi, regs.rdx, regs.r10, regs.r8, regs.r9};
                
                if (syscall_count < MAX_SYSCALLS) {
                    syscalls[syscall_count].syscall_num = syscall_num;
                    memcpy(syscalls[syscall_count].args, args, sizeof(args));
                }
                
                print_syscall(syscall_count, args);
                syscall_count++;
                in_syscall = 1;
            } 
            else {
                // Syscall exit
                long return_value = regs.rax;
                printf("  └─ returned: %ld\n", return_value);
                in_syscall = 0;
            }

            // Continue to next syscall
            if (ptrace(PTRACE_SYSCALL, child_pid, NULL, NULL) == -1) {
                perror("PTRACE_SYSCALL");
                break;
            }
        }

        printf("\n=====================================\n");
        printf("Total syscalls captured: %d\n", syscall_count);
    } 
    else {
        perror("fork failed");
        exit(1);
    }

    return 0;
}
