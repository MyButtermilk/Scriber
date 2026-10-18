#define _GNU_SOURCE
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/wait.h>
#include <unistd.h>

/* Linux wait4 resource observation, using the small GNU-time flag subset that
 * the study harness uses. ru_maxrss is a maximum, not a concurrent-tree sum. */
static volatile sig_atomic_t child_pid = 0;
static void forward_signal(int signal_number) {
    if (child_pid > 0) kill((pid_t)child_pid, signal_number);
}

int main(int argc, char **argv) {
    if (argc < 6 || strcmp(argv[1], "-f") || strcmp(argv[2], "%M") || strcmp(argv[3], "-o")) return 125;
    struct sigaction action = {0};
    action.sa_handler = forward_signal;
    sigemptyset(&action.sa_mask);
    sigaction(SIGINT, &action, NULL);
    sigaction(SIGTERM, &action, NULL);
    pid_t child = fork();
    if (child < 0) { perror("fork"); return 125; }
    if (child == 0) {
        execvp(argv[5], &argv[5]);
        perror("execvp");
        _exit(127);
    }
    child_pid = child;
    int status;
    struct rusage usage;
    pid_t waited;
    do { waited = wait4(child, &status, 0, &usage); } while (waited < 0 && errno == EINTR);
    if (waited < 0) { perror("wait4"); return 125; }
    child_pid = 0;
    FILE *output = fopen(argv[4], "w");
    if (output == NULL) { perror("fopen"); return 125; }
    if (fprintf(output, "%ld\n", usage.ru_maxrss) < 0 || fclose(output) != 0) return 125;
    if (WIFEXITED(status)) return WEXITSTATUS(status);
    return WIFSIGNALED(status) ? 128 + WTERMSIG(status) : 125;
}
