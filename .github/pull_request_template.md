## What and why

<!-- What changes, and what problem it solves. -->

## Verification

<!-- What did you actually run? "Tests pass" is good; "I drove the flow and saw
     X" is better. -->

- [ ] `python3 -m unittest discover -s tests` passes
- [ ] `MPB_DEMO=1 ./run.sh` still boots

## Scope check

- [ ] Adds no dependency, build step, or `requirements.txt`
- [ ] Adds no write path to the palace (`create=False` everywhere)
- [ ] Adds no outbound request beyond the documented PyPI version check
- [ ] Touches the `Host` allow-list / escaping / read-only guarantee? → a test covers it
