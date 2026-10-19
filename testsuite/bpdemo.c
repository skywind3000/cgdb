/* bpdemo.c -- a deterministic program for exercising gdb/cgdb breakpoints.
 *
 * Call layers (breakpoints bite at every level):
 *
 *     main -> process -> weighted_sum -> clamp
 *                        classify
 *
 * if/else chains, if/else inside for, and nested fors, all on fixed
 * inputs so the output is stable across runs.
 */
#include <stdio.h>

/* layer 3: if / else-if / else chain with early returns */
static int clamp(int v, int lo, int hi)
{
    if (v < lo)
        return lo;
    else if (v > hi)
        return hi;
    return v;
}

/* layer 2: for loop with if/else inside, then a tail call */
static int weighted_sum(int a, int b, int w)
{
    int total = 0;

    for (int i = 0; i < w; i++) {
        if (i % 2 == 0)
            total += a;
        else
            total += b;
    }
    return clamp(total, 0, 1000);
}

/* layer 2: a plain if / else-if ladder */
static int classify(int n)
{
    if (n < 0)
        return -1;
    else if (n == 0)
        return 0;
    else if (n % 3 == 0)
        return 1;
    else
        return 2;
}

/* layer 1: two nested calls inside a for, then if/else on the total */
static int process(int seed)
{
    int acc = 0;

    for (int i = 0; i < seed; i++) {
        int v = weighted_sum(i, i + 1, 3);

        acc += classify(v);
    }
    if (acc > 40)
        acc = clamp(acc, 0, 60);
    else
        acc = clamp(acc, 0, 30);
    return acc;
}

int main(void)
{
    int results[3];

    for (int i = 0; i < 3; i++)
        results[i] = process(5 + i * 3);

    printf("results: %d %d %d\n", results[0], results[1], results[2]);
    printf("MARKER total=%d\n", results[0] + results[1] + results[2]);
    return 0;
}
