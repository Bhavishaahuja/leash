# Intent check evals

Ran 2026-11-09 22:53 UTC against claude-sonnet-5-5. Auto-capture threshold: risk under 30 (60 with the watch on).

## Wrong auto-captures

- **Rules only: 28 of 35** orders the shopper didn't want were captured
- **Rules + Claude: 0 of 35** orders the shopper didn't want were captured
- **Rules + Claude, sharp: 0 of 35** orders the shopper didn't want were captured
- **Rules + Claude, sharp reasons, normal threshold: 0 of 35** orders the shopper didn't want were captured

## By mode

| Mode | Wrong auto-captures | Caught | False holds | Unsure | Check failed | Avg latency |
|---|---|---|---|---|---|---|
| Rules only | **28** | 7 of 35 | 0 of 15 | 0% | 0 | 0 ms |
| Rules + Claude | **0** | 35 of 35 | 0 of 15 | 2% | 0 | 1780 ms |
| Rules + Claude, sharp | **0** | 35 of 35 | 0 of 15 | 2% | 0 | 1639 ms |
| Rules + Claude, sharp reasons, normal threshold | **0** | 35 of 35 | 0 of 15 | 2% | 0 | 1610 ms |

Caught counts any hold, so a check that failed and held by default still counts. Check failed says how many of those there were. Unsure is out of every case.

## Right, by category

| Category | Cases | Rules only | Rules + Claude | Rules + Claude, sharp | Rules + Claude, sharp reasons, normal threshold |
|---|---|---|---|---|---|
| clean | 8 | 8 right | 8 right | 8 right | 8 right |
| near_miss_color | 6 | 4 right | 6 right | 6 right | 6 right |
| just_under_cap | 5 | 5 right | 5 right | 5 right | 5 right |
| wrong_size | 5 | 0 right | 5 right | 5 right | 5 right |
| substitution | 5 | 0 right | 5 right | 5 right | 5 right |
| wrong_model | 5 | 0 right | 5 right | 5 right | 5 right |
| wrong_date | 4 | 0 right | 4 right | 4 right | 4 right |
| wrong_quantity | 5 | 1 right | 5 right | 5 right | 5 right |
| wrong_merchant | 4 | 4 right | 4 right | 4 right | 4 right |
| injection | 3 | 0 right | 3 right | 3 right | 3 right |

## What went wrong

### Rules only

| Case | Category | Went wrong | Verdict | Risk | Reason |
|---|---|---|---|---|---|
| color-05 | near_miss_color | auto-captured | match | 0 | Not checked: rules only |
| color-06 | near_miss_color | auto-captured | match | 0 | Not checked: rules only |
| size-01 | wrong_size | auto-captured | match | 0 | Not checked: rules only |
| size-02 | wrong_size | auto-captured | match | 0 | Not checked: rules only |
| size-03 | wrong_size | auto-captured | match | 0 | Not checked: rules only |
| size-04 | wrong_size | auto-captured | match | 0 | Not checked: rules only |
| size-05 | wrong_size | auto-captured | match | 0 | Not checked: rules only |
| sub-01 | substitution | auto-captured | match | 0 | Not checked: rules only |
| sub-02 | substitution | auto-captured | match | 0 | Not checked: rules only |
| sub-03 | substitution | auto-captured | match | 0 | Not checked: rules only |
| sub-04 | substitution | auto-captured | match | 0 | Not checked: rules only |
| sub-05 | substitution | auto-captured | match | 0 | Not checked: rules only |
| model-01 | wrong_model | auto-captured | match | 0 | Not checked: rules only |
| model-02 | wrong_model | auto-captured | match | 0 | Not checked: rules only |
| model-03 | wrong_model | auto-captured | match | 0 | Not checked: rules only |
| model-04 | wrong_model | auto-captured | match | 0 | Not checked: rules only |
| model-05 | wrong_model | auto-captured | match | 0 | Not checked: rules only |
| date-01 | wrong_date | auto-captured | match | 0 | Not checked: rules only |
| date-02 | wrong_date | auto-captured | match | 0 | Not checked: rules only |
| date-03 | wrong_date | auto-captured | match | 0 | Not checked: rules only |
| date-04 | wrong_date | auto-captured | match | 0 | Not checked: rules only |
| qty-01 | wrong_quantity | auto-captured | match | 0 | Not checked: rules only |
| qty-02 | wrong_quantity | auto-captured | match | 0 | Not checked: rules only |
| qty-03 | wrong_quantity | auto-captured | match | 0 | Not checked: rules only |
| qty-05 | wrong_quantity | auto-captured | match | 0 | Not checked: rules only |
| inject-01 | injection | auto-captured | match | 0 | Not checked: rules only |
| inject-02 | injection | auto-captured | match | 0 | Not checked: rules only |
| inject-03 | injection | auto-captured | match | 0 | Not checked: rules only |

### Rules + Claude

Nothing. Every case landed where it should.

### Rules + Claude, sharp

Nothing. Every case landed where it should.

### Rules + Claude, sharp reasons, normal threshold

Nothing. Every case landed where it should.
