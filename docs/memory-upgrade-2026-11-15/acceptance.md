# English pilot acceptance

Each case was evaluated on the same immutable input data. Article groups were split before running the candidate. A result counts only when it returns all required in-scope evidence within the request budget, or correctly withholds material for a false premise.

| Evidence dimension | Development baseline → candidate | Holdout baseline → candidate |
|---|---:|---:|
| Facts | 3/4 → 4/4 | 2/2 → 2/2 |
| Change | 3/4 → 4/4 | 2/2 → 2/2 |
| Methods | 1/4 → 2/4 | 2/2 → 2/2 |
| Gotchas | 3/4 → 4/4 | 2/2 → 2/2 |
| Premise and scope | 1/4 → 4/4 | 0/2 → 2/2 |
| Total | 11/20 → 18/20 | 8/10 → 10/10 |

The two remaining development cases explicitly reported budget-limited results. Necessary sourced observations rose from 23/30 to 28/30. Wrong question scope fell from five cases to zero. Each latency mode used 90 samples per version. Process-cold p50/p95 moved from 97.172/141.795 ms to 100.166/168.208 ms. Warm p50/p95 moved from 3.977/8.944 ms to 4.184/6.575 ms. Both p95 checks passed the predeclared tolerance. The OS file cache remained available. No additional network retrieval requests occurred.

The tested export passed 74 targeted synthetic cases. A local full-suite comparison using matched fixtures had the same preexisting failures, with added behavior tested separately. The installed wrapper read the isolated candidate and the existing generation as expected. Full generated-answer quality and teaching latency still require a controlled comparison. The pilot made no formal writes.

Tests can be reproduced from the source branch with Python 3.10 or later.

```sh
python3 -m unittest tests.english_pipeline.test_memory_query tests.english_pipeline.test_memory_store tests.english_pipeline.test_memory_lifecycle
```

Baseline `4809ca08ce97a9ed4c3e2f5f4143973439188cdd`. Tested implementation `673e2f3dbc7bc91d2d13d5815074fd28dcd17b43`. The per-case inputs and outputs are private.
