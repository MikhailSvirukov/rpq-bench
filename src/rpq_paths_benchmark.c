//------------------------------------------------------------------------------
// rpq_paths_benchmark: benchmark prepared graph/NFA Matrix Market decompositions
//------------------------------------------------------------------------------
// SPDX-License-Identifier: BSD-2-Clause

#include "LAGraphX.h"
#include <errno.h>
#include <stdarg.h>
#include <inttypes.h>
#include <stdlib.h>
#include <string.h>

static bool process_progress = false ;

static void progress (const char *format, ...)
{
    if (!process_progress) return ;
    va_list args ;
    va_start (args, format) ;
    fputs ("[process] ", stderr) ;
    vfprintf (stderr, format, args) ;
    va_end (args) ;
    fputc ('\n', stderr) ;
    fflush (stderr) ;
}

typedef int (*QueryFunction)
(
    LAGraph_RpqPath **, size_t *, LAGraph_Graph *, size_t,
    const GrB_Index *, size_t, const GrB_Index *, size_t, LAGraph_Graph *,
    const GrB_Index *, size_t, char *
) ;

/** Read a Matrix Market matrix directly and materialize it by row.
 * @param[out] graph Receives an owned graph, or NULL on error.
 * @param[in] path Input filename.
 * @param[out] msg Optional LAGRAPH_MSG_LEN-byte error buffer.
 * @return GraphBLAS/LAGraph status; file errors return GrB_INVALID_VALUE.
 */
static int read_graph
(
    LAGraph_Graph *graph, const char *path, char *msg
)
{
    *graph = NULL ;
    progress ("matrix %s: opening", path) ;
    FILE *file = fopen (path, "r") ;
    if (file == NULL)
    {
        fprintf (stderr, "%s: %s\n", path, strerror (errno)) ;
        return (GrB_INVALID_VALUE) ;
    }
    GrB_Matrix A = NULL ;
    progress ("matrix %s: reading Matrix Market", path) ;
    int info = LAGraph_MMRead (&A, file, msg) ;
    fclose (file) ;
    if (info != GrB_SUCCESS) goto cleanup ;
    progress ("matrix %s: setting row-major storage", path) ;
    info = GrB_set (A, GrB_ROWMAJOR, GrB_STORAGE_ORIENTATION_HINT) ;
    if (info != GrB_SUCCESS) goto cleanup ;
    progress ("matrix %s: materializing", path) ;
    info = GrB_Matrix_wait (A, GrB_MATERIALIZE) ;
    if (info != GrB_SUCCESS) goto cleanup ;
    progress ("matrix %s: creating LAGraph graph", path) ;
    info = LAGraph_New (graph, &A, LAGraph_ADJACENCY_DIRECTED, msg) ;
cleanup:
    progress ("matrix %s: %s (info=%d)", path,
        info == GrB_SUCCESS ? "ready" : "failed", info) ;
    GrB_free (&A) ;
    return (info) ;
}

/** Read a counted list of one-based NFA states and convert them to zero-based.
 * @param[in] file Open metadata stream, positioned before the list's count.
 * @param[out] states Receives an owned array, including partial setup on error.
 * @param[out] count Receives the number of entries.
 * @param[out] msg Optional LAGRAPH_MSG_LEN-byte error buffer.
 * @return GrB_SUCCESS or an input/allocation error.
 */
static int read_states
(
    FILE *file, GrB_Index **states, size_t *count, char *msg
)
{
    if (fscanf (file, "%zu", count) != 1) return (GrB_INVALID_VALUE) ;
    int info = LAGraph_Malloc ((void **) states, *count, sizeof (GrB_Index), msg) ;
    if (info != GrB_SUCCESS) return (info) ;
    for (size_t k = 0 ; k < *count ; k++)
    {
        uint64_t state ;
        if (fscanf (file, "%" SCNu64, &state) != 1)
            return (GrB_INVALID_VALUE) ;
        (*states)[k] = state - 1 ;
    }
    return (GrB_SUCCESS) ;
}

typedef struct
{
    const char *label ;             // borrowed canonical label name from argv
    LAGraph_Graph graph ;           // owned; loaded once for the whole batch
    LAGraph_Graph transposed ;      // owned; materialized once when needed
}
GraphLabel ;

/** Materialize the other graph direction before any algorithm timing.
 * @param[out] result Receives an owned row-major graph; NULL on error.
 * @param[in] source Borrowed graph with Boolean-compatible adjacency values.
 * @param[out] msg Optional LAGRAPH_MSG_LEN-byte error buffer.
 */
static int transpose_graph
(
    LAGraph_Graph *result, LAGraph_Graph source, char *msg
)
{
    *result = NULL ;
    GrB_Matrix A = NULL ;
    GrB_Index n = 0 ;
    int info = GrB_Matrix_nrows (&n, source->A) ;
    if (info != GrB_SUCCESS) goto cleanup ;
    info = GrB_Matrix_new (&A, GrB_BOOL, n, n) ;
    if (info != GrB_SUCCESS) goto cleanup ;
    info = GrB_set (A, GrB_ROWMAJOR, GrB_STORAGE_ORIENTATION_HINT) ;
    if (info != GrB_SUCCESS) goto cleanup ;
    info = GrB_transpose (A, NULL, NULL, source->A, NULL) ;
    if (info != GrB_SUCCESS) goto cleanup ;
    info = GrB_Matrix_wait (A, GrB_MATERIALIZE) ;
    if (info != GrB_SUCCESS) goto cleanup ;
    info = LAGraph_New (result, &A, LAGraph_ADJACENCY_DIRECTED, msg) ;
cleanup:
    GrB_free (&A) ;
    return (info) ;
}

/** Load one query, call the API and release all query resources.
 * @param[in] prefix Query directory containing meta.txt and signed label matrices.
 * @param[in,out] graph Graph labels with lazily prepared owned transposes.
 * @param[in] graph_count Number of graph labels.
 * @param[in] query Selected public query function.
 * @param[out] seconds Receives the elapsed time of the public query call.
 * @param[out] count Receives the returned path count; valid only on success.
 * @param[out] msg Optional LAGRAPH_MSG_LEN-byte error buffer.
 * @return GrB_SUCCESS or a reading/allocation/algorithm error.
 * @pre Input correctness is verified by Python when requested by the caller.
 */
static int run_query
(
    const char *prefix, GraphLabel *graph,
    size_t graph_count, QueryFunction query,
    double *seconds, size_t *count, char *msg
)
{
    FILE *meta = NULL ;
    char *path = NULL ;
    GrB_Index *QS = NULL, *QF = NULL, *S = NULL ;
    LAGraph_Graph *G = NULL, *R = NULL ;
    LAGraph_RpqPath *paths = NULL ;
    size_t nqs = 0, nqf = 0, ns = 0, nl = 0, path_count = 0 ;
    uint64_t source = 0, target = 0 ;
    bool single_destination = false ;
    size_t path_size = strlen (prefix) + 64 ;
    int info = LAGraph_Malloc ((void **) &path, path_size, 1, msg) ;
    if (info != GrB_SUCCESS) goto cleanup ;
    snprintf (path, path_size, "%s/meta.txt", prefix) ;
    meta = fopen (path, "r") ;
    if (meta == NULL)
    {
        fprintf (stderr, "%s: %s\n", path, strerror (errno)) ;
        info = GrB_INVALID_VALUE ;
        goto cleanup ;
    }
    if (fscanf (meta, "%" SCNu64 " %" SCNu64, &source, &target) != 2)
    {
        info = GrB_INVALID_VALUE ;
        goto cleanup ;
    }
    single_destination = source == 0 && target != 0 ;
    info = read_states (meta, &QS, &nqs, msg) ;
    if (info != GrB_SUCCESS) goto cleanup ;
    info = read_states (meta, &QF, &nqf, msg) ;
    if (info != GrB_SUCCESS) goto cleanup ;
    if (fscanf (meta, "%zu", &nl) != 1)
    {
        info = GrB_INVALID_VALUE ;
        goto cleanup ;
    }
    info = LAGraph_Calloc ((void **) &G, nl, sizeof (LAGraph_Graph), msg) ;
    if (info != GrB_SUCCESS) goto cleanup ;
    info = LAGraph_Calloc ((void **) &R, nl, sizeof (LAGraph_Graph), msg) ;
    if (info != GrB_SUCCESS) goto cleanup ;
    for (size_t a = 0 ; a < nl ; a++)
    {
        int64_t label ;
        if (fscanf (meta, "%" SCNd64, &label) != 1)
        {
            info = GrB_INVALID_VALUE ;
            goto cleanup ;
        }
        char name [32] ;
        uint64_t magnitude = label < 0 ? -(uint64_t) label : (uint64_t) label ;
        snprintf (name, sizeof (name), "%" PRIu64, magnitude) ;
        for (size_t k = 0 ; k < graph_count ; k++)
        {
            if (strcmp (graph[k].label, name)) continue ;
            if (single_destination != (label < 0))
            {
                if (graph[k].transposed == NULL)
                {
                    progress ("graph label %s: materializing transpose", name) ;
                    info = transpose_graph (&graph[k].transposed, graph[k].graph, msg) ;
                    if (info != GrB_SUCCESS) goto cleanup ;
                }
                G[a] = graph[k].transposed ;
            }
            else G[a] = graph[k].graph ;
            break ;
        }
        snprintf (path, path_size, "%s/%" PRId64 ".txt", prefix, label) ;
        info = read_graph (&R[a], path, msg) ;
        if (info != GrB_SUCCESS) goto cleanup ;
        if (single_destination)
        {
            info = GrB_transpose (R[a]->A, NULL, NULL, R[a]->A, NULL) ;
            if (info != GrB_SUCCESS) goto cleanup ;
            info = GrB_Matrix_wait (R[a]->A, GrB_MATERIALIZE) ;
            if (info != GrB_SUCCESS) goto cleanup ;
        }
    }
    fclose (meta) ;
    meta = NULL ;
    GrB_Index ng = 0 ;
    info = GrB_Matrix_nrows (&ng, G[0]->A) ;
    if (info != GrB_SUCCESS) goto cleanup ;
    ns = source != 0 || single_destination ? 1 : (size_t) ng ;
    info = LAGraph_Malloc ((void **) &S, ns, sizeof (GrB_Index), msg) ;
    if (info != GrB_SUCCESS) goto cleanup ;
    if (source != 0) S[0] = source - 1 ;
    else if (single_destination) S[0] = target - 1 ;
    else for (size_t k = 0 ; k < ns ; k++) S[k] = k ;

    progress ("query %s: running algorithm", prefix) ;
    double begin = LAGraph_WallClockTime ( ) ;
    info = query (&paths, &path_count, R, nl,
        single_destination ? QF : QS, single_destination ? nqf : nqs,
        single_destination ? QS : QF, single_destination ? nqs : nqf,
        G, S, ns, msg) ;
    *seconds = LAGraph_WallClockTime ( ) - begin ;
    progress ("query %s: algorithm returned (info=%d)", prefix, info) ;
    if (info != GrB_SUCCESS) goto cleanup ;
    // SD paths follow the prepared graph from target to source.  This driver
    // exports counts only, so no reversal or copying of result nodes is needed.
    *count = path_count ;
cleanup:
    if (meta != NULL) fclose (meta) ;
    // Result descriptors share prefix nodes.  Release the whole result once
    // through LAGraph; individual tails/prefixes must not be freed separately.
    LAGraph_2RpqPath_FreePaths (&paths, path_count, NULL) ;
    if (R != NULL)
        for (size_t a = 0 ; a < nl ; a++) LAGraph_Delete (&R[a], NULL) ;
    LAGraph_Free ((void **) &path, NULL) ;
    LAGraph_Free ((void **) &QS, NULL) ;
    LAGraph_Free ((void **) &QF, NULL) ;
    LAGraph_Free ((void **) &S, NULL) ;
    LAGraph_Free ((void **) &G, NULL) ;
    LAGraph_Free ((void **) &R, NULL) ;
    if (info != GrB_SUCCESS && msg != NULL && msg[0] == '\0')
        snprintf (msg, LAGRAPH_MSG_LEN, "could not read or run query in %s", prefix) ;
    return (info) ;
}

// Python supplies: semantics warmup iterations graph_count
// (label graph_file)... query_prefixes...
int main (int argc, char **argv)
{
    if (argc < 8) return (EXIT_FAILURE) ;
    QueryFunction query = NULL ;
    if (!strcmp (argv[1], "all")) query = LAGraph_2RpqPath_AllPaths ;
    if (!strcmp (argv[1], "simple")) query = LAGraph_2RpqPath_AllSimple ;
    if (!strcmp (argv[1], "trails")) query = LAGraph_2RpqPath_AllTrails ;
    if (!strcmp (argv[1], "shortest")) query = LAGraph_2RpqPath_AllShortestPaths ;
    char *end = NULL ;
    errno = 0 ;
    long warmup = strtol (argv[2], &end, 10) ;
    if (errno || *end != '\0' || end == argv[2] || warmup < 0)
        return (EXIT_FAILURE) ;
    long iterations = strtol (argv[3], &end, 10) ;
    if (errno || *end != '\0' || end == argv[3] || iterations < 1)
        return (EXIT_FAILURE) ;
    long n = strtol (argv[4], &end, 10) ;
    if (errno || query == NULL || *end != '\0' || n < 1 || n > (argc - 6) / 2)
        return (EXIT_FAILURE) ;
    size_t first_query = 5 + 2 * n ;
    size_t query_count = argc - first_query ;
    char msg [LAGRAPH_MSG_LEN] = "" ;
    GraphLabel *graph = NULL ;
    bool failed = false ;
    FILE *samples = NULL ;
    const char *samples_path = getenv ("RPQ_BENCH_VERIFY_SAMPLES") ;
    process_progress = getenv ("RPQ_BENCH_PROCESS") != NULL ;
    bool trace_queries = process_progress || getenv ("LAGRAPH_RPQ_TRACE") != NULL ;
    progress ("initializing LAGraph/GraphBLAS") ;
    int info = LAGraph_Init (msg) ;
    if (info != GrB_SUCCESS)
    {
        fprintf (stderr, "initialization: info=%d %s\n", info, msg) ;
        return (EXIT_FAILURE) ;
    }
    progress ("initialization complete") ;
    if (samples_path != NULL)
    {
        samples = fopen (samples_path, "w") ;
        if (samples == NULL)
        {
            fprintf (stderr, "%s: %s\n", samples_path, strerror (errno)) ;
            info = GrB_INVALID_VALUE ;
            goto cleanup ;
        }
    }
    info = LAGraph_Calloc ((void **) &graph, (size_t) n, sizeof (GraphLabel), msg) ;
    if (info != GrB_SUCCESS) goto cleanup ;
    for (long k = 0 ; k < n ; k++)
    {
        progress ("loading graph matrix %ld/%ld", k + 1, n) ;
        graph[k].label = argv[5 + 2 * k] ;
        info = read_graph (&graph[k].graph, argv[6 + 2 * k], msg) ;
        if (info != GrB_SUCCESS) goto cleanup ;
    }
    progress ("graph loading complete") ;
    if (printf ("query\ttime_seconds\tpaths\n") < 0 || fflush (stdout) != 0)
    {
        fprintf (stderr, "could not write results: %s\n", strerror (errno)) ;
        failed = true ;
        goto cleanup ;
    }
    // Finish every warmup batch before starting measured batches.  Keeping
    // phases separate also avoids overflowing warmup + iterations.
    for (int phase = 0 ; phase < 2 ; phase++)
    {
        long passes = phase == 0 ? warmup : iterations ;
        for (long pass = 0 ; pass < passes ; pass++)
        {
            for (size_t q = 0 ; q < query_count ; q++)
            {
                const char *prefix = argv[first_query + q] ;
                const char *number = strrchr (prefix, '/') ;
                number = number == NULL ? prefix : number + 1 ;
                double seconds = 0 ;
                size_t count = 0 ;
                msg[0] = '\0' ;
                if (trace_queries)
                {
                    fprintf (stderr, "query %s: %s pass %ld\n", number,
                        phase == 0 ? "warmup" : "measured", pass + 1) ;
                    fflush (stderr) ;
                }
                int query_info = run_query (prefix, graph, (size_t) n, query,
                    &seconds, &count, msg) ;
                if (query_info != GrB_SUCCESS)
                {
                    fprintf (stderr, "%s: %s pass %ld: info=%d %s\n",
                        number, phase == 0 ? "warmup" : "measured",
                        pass + 1, query_info, msg) ;
                    failed = true ;
                    continue ;
                }
                if (phase == 0) continue ;
                // Export observations only; Python performs optional verification.
                // Publish each measured invocation before starting the next query.
                // Output and flushing are outside the timed public API call.
                if (printf ("%s\t%.9f\t%zu\n", number, seconds, count) < 0 ||
                    fflush (stdout) != 0)
                {
                    fprintf (stderr, "could not write results: %s\n", strerror (errno)) ;
                    failed = true ;
                    goto cleanup ;
                }
                if (samples != NULL &&
                    (fprintf (samples, "%s\t%zu\n", number, count) < 0 ||
                     fflush (samples) != 0))
                {
                    fprintf (stderr, "%s: %s\n", samples_path, strerror (errno)) ;
                    failed = true ;
                    goto cleanup ;
                }
            }
        }
    }
cleanup:
    if (samples != NULL && fclose (samples) != 0)
    {
        fprintf (stderr, "%s: %s\n", samples_path, strerror (errno)) ;
        failed = true ;
    }
    if (graph != NULL)
        for (long k = 0 ; k < n ; k++)
        {
            LAGraph_Delete (&graph[k].transposed, NULL) ;
            LAGraph_Delete (&graph[k].graph, NULL) ;
        }
    LAGraph_Free ((void **) &graph, NULL) ;
    if (info != GrB_SUCCESS) fprintf (stderr, "info=%d %s\n", info, msg) ;
    LAGraph_Finalize (NULL) ;
    return (failed || info != GrB_SUCCESS ? EXIT_FAILURE : EXIT_SUCCESS) ;
}
