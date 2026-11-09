/* Catch broken static/PIE startup before compiling all FRR daemons. */
#include <pthread.h>
#include <stdio.h>

static _Thread_local volatile int thread_value = 42;

static void *check_thread(void *unused)
{
    (void)unused;
    if (thread_value != 42)
        return (void *)1;
    thread_value = 7;
    return thread_value == 7 ? NULL : (void *)1;
}

int main(void)
{
    pthread_t thread;
    void *result;
    if (thread_value != 42)
        return 1;
    if (pthread_create(&thread, NULL, check_thread, NULL))
        return 2;
    if (pthread_join(thread, &result) || result || thread_value != 42)
        return 3;
    puts("musl startup/TLS smoke passed");
    return 0;
}
