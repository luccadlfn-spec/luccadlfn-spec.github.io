#!/bin/bash
# Hostinger "post-install script" for myai.
#
# Save this file as a post-install script in hPanel and select it when you
# install Ubuntu 24.04 on the VPS. Hostinger runs it once, as root, right
# after the system is set up. It installs myai without asking anything.
#
#   Watch the progress:   tail -f /root/myai-install.log
#   When it is finished:  cat /root/myai-credentials.txt
#
# Optional: set your own values by removing the "#" in front of a line.
# export MYAI_DOMAIN=chat.example.com
# export MYAI_ADMIN_EMAIL=you@example.com
# export MYAI_MODEL=hauhau

set -u
umask 077 # the log will contain the first password
LOG=/root/myai-install.log
REPO=${MYAI_REPO:-luccadlfn-spec/luccadlfn-spec.github.io}
exec >>"$LOG" 2>&1
echo "=== myai post-install started: $(date) ==="

# Right after the first boot the network can need a moment.
tries=0
until curl -sS -o /dev/null --max-time 10 https://raw.githubusercontent.com/ || [ "$tries" -ge 30 ]; do
  tries=$((tries + 1))
  sleep 10
done

# The installer is saved to a file first, so that a download that was cut
# off is noticed before anything runs.
for ref in ${MYAI_REF:-ccr-f9008ee5-63plj0 main}; do
  url="https://raw.githubusercontent.com/$REPO/$ref/ai-server/install.sh"
  if curl -fsSL --retry 5 --retry-delay 10 -o /root/myai-install.sh "$url"; then
    echo "Installer downloaded from $url"
    MYAI_REF=$ref bash /root/myai-install.sh --yes
    status=$?
    echo "=== myai post-install finished with status $status: $(date) ==="
    exit "$status"
  fi
done

echo "The installer could not be downloaded. Run it by hand in the browser terminal:"
echo "  curl -fsSL https://raw.githubusercontent.com/$REPO/main/ai-server/install.sh | bash"
exit 1
