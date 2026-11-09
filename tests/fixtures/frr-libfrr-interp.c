/* Minimal surrounding function; the following .interp block is from FRR 10.4.1. */
#define NULL ((void *)0)
#define INTERP "/lib/ld-musl-x86_64.so.1"
void placeholder(void **statep)
{
	*statep = NULL;
}

#ifdef INTERP
static const char interp[]
	__attribute__((section(".interp"), used)) = INTERP;
#endif
/*
 * executable entry point for libfrr.so
 *
 */
