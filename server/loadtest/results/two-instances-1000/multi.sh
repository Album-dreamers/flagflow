#!/usr/bin/env bash
# two http instances, 500 clients on each, one publisher
D=/data/flagflow/.devenv; SP=/tmp/claude-0/-data/a92ac85b-873e-4072-9e66-81de36c86f1a/scratchpad
cd /data/flagflow/server
export PGUSER=postgres PGPASS=postgres SECRET=023517f06f444118986877e636b4a226 LOCUST=$D/bench-venv/bin/locust LOADTEST_PRELOAD=0 LOADTEST_SYNC=1 RATE=100
OUTA=$SP/lt-multi-a; OUTB=$SP/lt-multi-b; rm -rf $OUTB; mkdir -p $OUTB
CONFIG_PATH=$D/bench-8082.yaml .venv/bin/python -m featureflags http > $OUTB/server.log 2>&1 &
SB=$!; sleep 4
LOADTEST_OUT=$OUTB $LOCUST -f loadtest/locustfile.py --headless -u 500 -r 100 -t 70s --processes 1 \
    --master-bind-port 5567 --master-port 5567 -H http://127.0.0.1:8082 --only-summary > $OUTB/locust.log 2>&1 &
LB=$!; sleep 1
CONFIG_PATH=$D/bench.yaml OUT=$OUTA timeout 300 loadtest/run.sh 500 1 5 10 > $SP/multi-a.txt 2>&1
timeout 60 tail --pid=$LB -f /dev/null; sleep 3; kill $SB; wait $SB 2>/dev/null
for x in a b; do echo "$x: $(cat $SP/lt-multi-$x/recv-*.csv | grep -v '^user' | cut -d, -f1 | sort -u | wc -l) users, server errors $(grep -c 'Exception in ASGI' $SP/lt-multi-$x/server.log), zmq errors $(grep -c ZMQError $SP/lt-multi-$x/locust.log)"; done
rm -rf $SP/lt-multi-all && mkdir -p $SP/lt-multi-all && cp $OUTA/published.csv $SP/lt-multi-all/
for x in a b; do for f in $SP/lt-multi-$x/recv-*.csv; do cp $f $SP/lt-multi-all/$x-$(basename $f); done; done
$D/bench-venv/bin/python loadtest/report.py $SP/lt-multi-all --users 1000
echo MULTI-DONE
