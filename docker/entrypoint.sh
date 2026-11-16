#!/bin/sh
# Container entrypoint. Started as root (the default), it gives the service user
# (uid and gid 10001) the data directory, checks that this user can use it, and
# runs the command as that user. Started as another user (`user:` in compose),
# it only checks. See "Data directory and file ownership" in README.md.
set -eu

data_dir=/usr/local/lib/auth-master/data_dir.py

if [ "$(id -u)" = 0 ]; then
    python -I "$data_dir" take-ownership
    setpriv --reuid=10001 --regid=10001 --clear-groups --inh-caps=-all --no-new-privs -- \
        python -I "$data_dir" check
    exec setpriv --reuid=10001 --regid=10001 --clear-groups --inh-caps=-all --no-new-privs -- "$@"
fi

python -I "$data_dir" check
exec "$@"
