#!/bin/bash

set -o pipefail

PATH="/usr/local/bin:/bin:/usr/bin:/sbin:/usr/local/sbin:/usr/sbin:/opt/ibutils/bin:"

if [ $# -ne 1 ]
then
        echo $0: usage: $0 'on|1' to enable hyper-threading.
        echo $0: usage: $0 'off|0' to disable hyper-threading.
        echo $0: usage: $0 'show' to show hyper-threading.
        exit 1
fi

if [ `id -u` -ne 0 ]
then
        echo $0: you need to be root
        exit 1
fi

# VM SMT is controlled at OCI launch time. In particular, never offline Intel
# VM siblings or re-enable them through an old unit's ExecStop=... on.
virtualization=$(systemd-detect-virt --vm)
virtualization_status=$?
if [ "$virtualization_status" -eq 0 ] && [ -n "$virtualization" ] && [ "$virtualization" != none ]; then
        echo "$0: VM detected ($virtualization); guest HT control is disabled"
        exit 0
fi
if [ "$virtualization_status" -ne 1 ] || [ "$virtualization" != none ]; then
        echo "$0: cannot confirm bare metal; refusing to change CPU state" >&2
        exit 1
fi

disable_ht() {
	local secondary_threads thread
	echo -n $0: disabling 
        # Linux CPU lists can contain individual IDs, ranges, or both. Keep
        # the first logical CPU in each core and offline every sibling.
        secondary_threads=$(cat /sys/devices/system/cpu/cpu*/topology/thread_siblings_list | sort --unique --numeric-sort | awk -F, '{
                first = 1
                for (field = 1; field <= NF; field++) {
                        count = split($field, range, "-")
                        last = count == 2 ? range[2] : range[1]
                        for (cpu = range[1]; cpu <= last; cpu++) {
                                if (first) first = 0
                                else print cpu
                        }
                }
        }') || return 1
        for thread in $secondary_threads
        do
                echo -n ' ' cpu"$thread"
                echo 0 > "/sys/devices/system/cpu/cpu$thread/online" || return 1
        done
	echo
}

enable_ht() {
	echo -n $0: enabling 
        for f in `echo /sys/devices/system/cpu/cpu[0-9]*`
        do
                # CPU 0 can lack an online file when it cannot be hotplugged.
                [ -e "$f/online" ] || continue
                __enabled=$(cat "$f/online") || return 1
                if [ "$__enabled" -eq 0 ]
                then
                        echo -n ' ' `basename $f`
                        echo 1 > "$f/online" || return 1
                fi
        done
	echo ''
}

#
# rebalance_irqs() {
# 	echo -n rebalancing IRQs
# 	systemctl restart irqbalance
# 	echo -n '.'; sleep 1
# 	echo -n '.'; sleep 1
# 	echo -n '.'; sleep 1
# 	echo ''
# }
#

case "$1" in
"1"|"on")
        enable_ht || exit $?
        #rebalance_irqs
        ;;
"0"|"off")
        disable_ht || exit $?
        #rebalance_irqs
        ;;
"show")
	;;
*)
        echo $0: wrong argument "$1"
        exit 2
        ;;
esac

echo ''
lscpu | egrep "On-line|Off-line"

exit 0
