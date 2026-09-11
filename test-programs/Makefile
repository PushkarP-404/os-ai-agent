CC ?= gcc
CFLAGS ?= -Wall -O2 -I../custom-kernel/include

all: test_syscall

test_syscall: test_syscall.c
	$(CC) $(CFLAGS) -o test_syscall test_syscall.c

clean:
	rm -f test_syscall
