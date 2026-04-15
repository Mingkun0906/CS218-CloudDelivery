## What changed
<!-- One paragraph: what this PR does and why -->

## How to test
<!-- Commands or steps to verify the change works -->
```bash
# e.g.
pytest tests/unit/test_matching.py -v
sam local invoke MatchingLambda -e events/sample_order.json
```

## AWS resources affected
<!-- List any resources created, modified, or deleted -->
- [ ] No AWS resource changes
- [ ] DynamoDB schema change
- [ ] Kinesis stream config change
- [ ] IAM policy change
- [ ] Other: ___

## Checklist
- [ ] `black` and `flake8` pass with no errors
- [ ] Unit tests pass (`pytest tests/unit/`)
- [ ] `config/dev.json` updated if new env vars added
- [ ] `INTERFACE_SPEC.md` updated if any contract changed
- [ ] Other person tagged for review
