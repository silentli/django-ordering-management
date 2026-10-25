# CI autofix acceptance packages

These healthy, isolated examples exercise fractional timeout logic, setuptools package discovery,
and a declared PyYAML dependency. They do not affect the Django application. Package CI uses one
runner for all three: pinned build tools, a real wheel, a separate clean application environment,
and installed-package unittest contracts. No editable install or source PYTHONPATH is used.

This bootstrap adds no deliberate failure. Later acceptance faults and repairs require separate
reviewed PRs; the local Agent cannot edit these contracts, runner, workflow, or dependency policy.
The self-hosted listener never executes this repository runner on the Mac.
