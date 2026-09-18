CC ?= gcc
CFLAGS ?= -Wall -O2 -I../custom-kernel/include

all: test_syscall test_syscall_stress

test_syscall: test_syscall.c
	$(CC) $(CFLAGS) -o test_syscall test_syscall.c

test_syscall_stress: test_syscall_stress.c
	$(CC) $(CFLAGS) -pthread -o test_syscall_stress test_syscall_stress.c

clean:
	rm -f test_syscall test_syscall_stress
