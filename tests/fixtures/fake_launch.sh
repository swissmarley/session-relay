#!/bin/sh
# Test double for launch.sh: records its argv, prints a method, exits with FAKE_LAUNCH_RC.
printf '%s\n' "$@" > "${FAKE_LAUNCH_LOG:-/dev/null}"
printf 'launched fake\n'
exit "${FAKE_LAUNCH_RC:-0}"
