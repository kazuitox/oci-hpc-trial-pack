#!/bin/bash
if [ `id -u` -ne 0 ]
then
        echo $0: you need to be root
        exit 1
fi

set_ht() {
        local current_state
        if [ ! -e /sys/devices/system/cpu/smt/control ]; then
                echo "$0: SMT control is unavailable; no guest change needed"
                return 0
        fi
        current_state=$(cat /sys/devices/system/cpu/smt/control) || return 1
        case "$current_state" in
        "$1"|forceoff|notsupported|notimplemented)
                # Launch-time HT off can expose a guest without SMT support.
                # Kernels without runtime SMT control also reject writes.
                echo "$0: SMT is $current_state; no guest change needed"
                return 0
                ;;
        esac
        echo "$1" | sudo tee /sys/devices/system/cpu/smt/control
}

disable_ht() {
        echo -n $0: disabling
        set_ht off
}

enable_ht() {
        echo -n $0: enabling
        set_ht on
}

case "$1" in
"1"|"on")
        enable_ht || exit $?
        ;;
"0"|"off")
        disable_ht || exit $?
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
