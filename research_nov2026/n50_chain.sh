#!/bin/bash
# NIFTY 50 universe: price-only variants + robustness now; Kronos reads (only the missing signal days) once the top-60 job is done
cd /opt/kronos_bot/research
PY=/opt/kronos_bot/venv/bin/python
export STK_UNIV=/opt/kronos_bot/research/nifty50_syms.json
$PY -W ignore stk_triggers.py 100 > stk_triggers_n50.log 2>&1
$PY -W ignore stk_v1_robust.py > stk_v1_robust_n50.log 2>&1
until grep -q KRONOS_READS_COMPLETE stk_kronos_reads.log; do sleep 20; done
$PY -W ignore stk_kronos_reads.py > stk_kronos_reads_n50.log 2>&1
$PY -W ignore stk_kronos_filter.py > stk_kronos_filter_n50.log 2>&1
echo CHAIN_DONE > n50_chain.done
