# -*- coding: utf-8 -*-
"""
Hypothetical P&L simulation — ORB-only, no secondary filters
Wed Apr 29 2026
"""
import math

def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def bs_call(S, K, T, r, sigma):
    if T <= 1e-8:
        return max(S - K, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return S * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)

r = 0.065

# ─── Spot price series (time, spot, hours_since_entry) ────────────────────────

nifty_bars = [
    ("09:45",24141.35,0.0),("09:50",24164.80,5/60),("09:55",24173.95,10/60),
    ("10:00",24173.20,15/60),("10:05",24201.30,20/60),("10:10",24211.75,25/60),
    ("10:15",24233.80,30/60),("10:20",24243.10,35/60),("10:25",24270.45,40/60),
    ("10:30",24290.15,45/60),("10:35",24283.20,50/60),("10:40",24268.70,55/60),
    ("10:45",24267.45,60/60),("10:50",24271.65,65/60),("10:55",24285.05,70/60),
    ("11:00",24287.10,75/60),("11:05",24277.05,80/60),("11:10",24292.60,85/60),
    ("11:15",24288.15,90/60),("11:20",24276.10,95/60),("11:25",24277.35,100/60),
    ("11:30",24263.25,105/60),("11:35",24278.15,110/60),("11:40",24276.60,115/60),
    ("11:45",24271.85,120/60),("11:50",24267.05,125/60),("11:55",24271.05,130/60),
    ("12:00",24276.95,135/60),("12:05",24273.05,140/60),("12:10",24283.00,145/60),
    ("12:15",24293.15,150/60),("12:20",24301.55,155/60),("12:25",24310.45,160/60),
    ("12:30",24307.10,165/60),("12:35",24324.70,170/60),("12:40",24328.60,175/60),
    ("12:45",24321.60,180/60),("12:50",24321.30,185/60),("13:00",24316.85,195/60),
    ("13:05",24317.40,200/60),("13:10",24319.20,205/60),("13:15",24318.20,210/60),
    ("13:20",24320.85,215/60),("13:25",24310.20,220/60),("13:30",24305.20,225/60),
    ("13:35",24313.10,230/60),("13:40",24285.00,235/60),("13:45",24272.55,240/60),
    ("13:50",24266.05,245/60),("13:55",24269.85,250/60),("14:00",24254.95,255/60),
    ("14:05",24202.45,260/60),("14:10",24207.20,265/60),("14:15",24214.60,270/60),
    ("14:20",24189.30,275/60),("14:25",24214.45,280/60),("14:30",24186.70,285/60),
]

bnf_bars = [
    ("10:05",55719.15,0.0),("10:10",55734.15,5/60),("10:15",55812.60,10/60),
    ("10:20",55835.35,15/60),("10:25",55875.35,20/60),("10:30",55904.15,25/60),
    ("10:35",55854.55,30/60),("10:40",55813.75,35/60),("10:45",55814.40,40/60),
    ("10:50",55844.90,45/60),("10:55",55896.40,50/60),("11:00",55883.45,55/60),
    ("11:05",55778.60,60/60),("11:10",55868.30,65/60),("11:15",55855.40,70/60),
    ("11:20",55873.95,75/60),("11:25",55941.45,80/60),("11:30",55910.80,85/60),
    ("11:35",55929.25,90/60),("11:40",55900.75,95/60),("11:45",55885.70,100/60),
    ("11:50",55870.85,105/60),("11:55",55884.65,110/60),("12:00",55895.85,115/60),
    ("12:05",55906.45,120/60),("12:10",55928.00,125/60),("12:15",55992.50,130/60),
    ("12:20",56014.35,135/60),("12:25",56052.50,140/60),("12:30",56081.15,145/60),
    ("12:35",56123.45,150/60),("12:40",56141.05,155/60),("12:45",56143.30,160/60),
    ("12:50",56132.10,165/60),("12:55",56118.35,170/60),("13:00",56083.25,175/60),
    ("13:05",56108.80,180/60),("13:10",56088.85,185/60),("13:15",56069.00,190/60),
    ("13:20",56073.90,195/60),("13:25",56051.15,200/60),("13:30",56011.60,205/60),
    ("13:35",56024.70,210/60),("13:40",55947.85,215/60),("13:45",55943.90,220/60),
    ("13:50",55929.35,225/60),("13:55",55933.55,230/60),("14:00",55861.75,235/60),
    ("14:05",55704.85,240/60),("14:10",55706.50,245/60),("14:15",55663.40,250/60),
    ("14:20",55544.45,255/60),("14:25",55606.35,260/60),("14:30",55489.20,265/60),
]

snx_bars = [
    ("09:45",77378.07,0.0),("09:50",77448.07,5/60),("09:55",77472.00,10/60),
    ("10:00",77456.43,15/60),("10:05",77538.05,20/60),("10:10",77589.05,25/60),
    ("10:15",77670.17,30/60),("10:20",77709.44,35/60),("10:25",77801.79,40/60),
    ("10:30",77851.96,45/60),("10:35",77843.33,50/60),("10:40",77769.60,55/60),
    ("10:45",77747.55,60/60),("10:50",77772.28,65/60),("10:55",77817.48,70/60),
    ("11:00",77830.03,75/60),("11:05",77808.57,80/60),("11:10",77856.22,85/60),
    ("11:15",77841.62,90/60),("11:20",77799.29,95/60),("11:25",77799.15,100/60),
    ("11:30",77767.23,105/60),("11:35",77802.60,110/60),("11:40",77802.02,115/60),
    ("11:45",77788.51,120/60),("11:50",77760.38,125/60),("11:55",77767.75,130/60),
    ("12:00",77791.47,135/60),("12:05",77778.95,140/60),("12:10",77809.86,145/60),
    ("12:15",77848.84,150/60),("12:20",77879.47,155/60),("12:25",77906.15,160/60),
    ("12:30",77895.99,165/60),("12:36",77944.02,171/60),("12:40",77966.20,175/60),
    ("12:45",77935.09,180/60),("12:50",77939.34,185/60),("12:55",77939.52,190/60),
    ("13:00",77916.50,195/60),("13:05",77927.15,200/60),("13:10",77942.22,205/60),
    ("13:15",77941.77,210/60),("13:20",77941.15,215/60),("13:25",77906.72,220/60),
    ("13:30",77902.64,225/60),("13:35",77919.66,230/60),("13:40",77827.24,235/60),
    ("13:45",77800.99,240/60),("13:50",77790.25,245/60),("13:55",77803.06,250/60),
    ("14:00",77743.43,255/60),("14:05",77563.52,260/60),("14:10",77591.07,265/60),
    ("14:15",77607.27,270/60),("14:20",77541.77,275/60),("14:25",77601.55,280/60),
    ("14:30",77514.38,285/60),
]


def simulate(bars, K, sigma, lot, T_entry_h, chk="10:55", name=""):
    T_entry = T_entry_h / (365 * 24)
    S0 = bars[0][1]
    P0 = bs_call(S0, K, T_entry, r, sigma)

    stop_p   = P0 * 0.75
    target_p = P0 * 1.28
    trail_tr = P0 * 1.18

    P_peak = P0
    trail_active = False
    trail_stop   = 0.0
    chk_done     = False

    W = 65
    print()
    print("=" * W)
    print(f"  {name}  |  K={K}  σ={sigma:.0%}  Lot={lot}")
    print(f"  Entry {bars[0][0]}: Spot={S0:,.0f}  Option≈₹{P0:.1f}  (cost 1 lot=₹{P0*lot:,.0f})")
    print(f"  Stop=₹{stop_p:.1f}  Target=₹{target_p:.1f}  TrailTrigger=₹{trail_tr:.1f}")
    print("-" * W)
    fmt = "  {:6} | {:>9} | {:>7} | {:>6} | {}"
    print(fmt.format("Time", "Spot", "OPT(₹)", "Gain%", "Note"))
    print("  " + "-" * (W - 2))

    result = None
    for i, (t, S, elapsed_h) in enumerate(bars):
        T = T_entry - elapsed_h / (365 * 24)
        P = bs_call(S, K, T, r, sigma)
        gain = (P / P0 - 1) * 100

        if P > P_peak:
            P_peak = P
            if trail_active:
                trail_stop = P_peak * 0.90

        note = ""
        # Print entry, checkpoint, force-close, every 30min, and key events
        show = (i == 0 or t == chk or t == "14:30" or int(elapsed_h * 60) % 30 == 0)

        # Checkpoint
        if t == chk and not chk_done:
            chk_done = True
            if gain < 0:
                note = f"CHKPT LOSS → CLOSE (gain={gain:.1f}%)"
                if show: print(fmt.format(t, f"{S:,.0f}", f"{P:.1f}", f"{gain:+.1f}%", note))
                result = (t, P, "CHECKPOINT-LOSS", gain)
                break
            elif gain >= 15.0:
                trail_active = True
                trail_stop = P_peak * 0.90
                note = f"CHKPT → HOLD+TRAIL  trail_stop=₹{trail_stop:.1f}"
                print(fmt.format(t, f"{S:,.0f}", f"{P:.1f}", f"{gain:+.1f}%", note))
                continue
            else:
                note = f"CHKPT PROFIT < 15% → CLOSE"
                if show: print(fmt.format(t, f"{S:,.0f}", f"{P:.1f}", f"{gain:+.1f}%", note))
                result = (t, P, "CHECKPOINT-PROFIT-CLOSE", gain)
                break

        if show and not result:
            print(fmt.format(t, f"{S:,.0f}", f"{P:.1f}", f"{gain:+.1f}%", note))

        # Exits
        if P <= stop_p:
            note = f"STOP-LOSS  opt={P:.1f} ≤ {stop_p:.1f}"
            print(fmt.format(t, f"{S:,.0f}", f"{P:.1f}", f"{gain:+.1f}%", note))
            result = (t, P, "STOP-LOSS", gain); break

        if not trail_active and P >= target_p:
            note = f"TARGET +28%  opt={P:.1f} ≥ {target_p:.1f}"
            print(fmt.format(t, f"{S:,.0f}", f"{P:.1f}", f"{gain:+.1f}%", note))
            result = (t, P, "TARGET", gain); break

        if trail_active and P <= trail_stop:
            note = f"TRAIL EXIT  opt={P:.1f} ≤ trail={trail_stop:.1f}  peak=₹{P_peak:.1f}"
            print(fmt.format(t, f"{S:,.0f}", f"{P:.1f}", f"{gain:+.1f}%", note))
            result = (t, P, "TRAIL", gain); break

        if t == "14:30":
            note = "FORCE-CLOSE 14:30"
            print(fmt.format(t, f"{S:,.0f}", f"{P:.1f}", f"{gain:+.1f}%", note))
            result = (t, P, "FORCE-CLOSE", gain); break

    if not result:
        t2, S2, eh2 = bars[-1]
        T2 = T_entry - eh2 / (365 * 24)
        P2 = bs_call(S2, K, T2, r, sigma)
        g2 = (P2 / P0 - 1) * 100
        result = (t2, P2, "END", g2)

    t_exit, P_exit, reason, gain_exit = result
    pnl_lot   = (P_exit - P0) * lot
    print("-" * W)
    print(f"  EXIT at {t_exit}: {reason}  gain={gain_exit:+.1f}%")
    print(f"  Entry ₹{P0:.1f}  →  Exit ₹{P_exit:.1f}")
    print(f"  P&L (1 lot, {lot} units): ₹{pnl_lot:+,.0f}")
    return pnl_lot, P0, P_exit, reason


def simulate_trail(bars, K, sigma, lot, T_entry_h, chk="10:55", name=""):
    """Trail-only mode: checkpoint always says HOLD, no fixed target."""
    T_entry = T_entry_h / (365 * 24)
    S0, P0 = bars[0][1], bs_call(bars[0][1], K, T_entry, r, sigma)
    stop_p = P0 * 0.75
    P_peak = P0; trail_active = False; trail_stop = 0.0
    print(f"\n  {name}  | entry=Rs{P0:.1f}  stop=Rs{stop_p:.1f}")
    result = None
    for t, S, eh in bars:
        T = T_entry - eh / (365 * 24)
        P = bs_call(S, K, T, r, sigma)
        gain = (P / P0 - 1) * 100
        if P > P_peak:
            P_peak = P
            if trail_active:
                trail_stop = P_peak * 0.90
        if t == chk:
            trail_active = True; trail_stop = P_peak * 0.90
            print(f"    {t}: Rs{P:.1f} ({gain:+.1f}%)  HOLD+TRAIL  stop=Rs{trail_stop:.1f}")
            continue
        if P <= stop_p:
            print(f"    {t}: Rs{P:.1f} ({gain:+.1f}%)  STOP  PnL=Rs{(P-P0)*lot:+,.0f}")
            return (P - P0) * lot
        if trail_active and P <= trail_stop:
            print(f"    {t}: Rs{P:.1f} ({gain:+.1f}%)  TRAIL EXIT (peak=Rs{P_peak:.1f})  PnL=Rs{(P-P0)*lot:+,.0f}")
            return (P - P0) * lot
        if t == "14:30":
            print(f"    {t}: Rs{P:.1f} ({gain:+.1f}%)  FORCE-CLOSE  PnL=Rs{(P-P0)*lot:+,.0f}")
            return (P - P0) * lot
    t2, S2, eh2 = bars[-1]
    P2 = bs_call(S2, K, T_entry - eh2/(365*24), r, sigma)
    return (P2 - P0) * lot


print()
print("★" * 65)
print("  HYPOTHETICAL ORB-ONLY P&L — Wed Apr 29 2026")
print("  (all secondary filters bypassed; 1 lot per instrument)")
print("★" * 65)

# NIFTY: Thu Apr 30 expiry (12h from 09:45)
pnl_nf, _, _, _ = simulate(
    nifty_bars, K=24250, sigma=0.22, lot=65,
    T_entry_h=12.0, chk="10:55",
    name="NIFTY  24250 CE  [Thu Apr 30 expiry]"
)

# BNF: Fri May 1 expiry (17.92h from 10:05)
pnl_bnf, _, _, _ = simulate(
    bnf_bars, K=55900, sigma=0.22, lot=30,
    T_entry_h=17.92, chk="10:55",
    name="BNF    55900 CE  [Fri May 1 expiry]"
)

# SENSEX: Fri May 1 expiry (18.25h from 09:45)
pnl_snx, _, _, _ = simulate(
    snx_bars, K=77400, sigma=0.15, lot=20,
    T_entry_h=18.25, chk="10:55",
    name="SENSEX 77400 CE  [Fri May 1 expiry]"
)

total = pnl_nf + pnl_bnf + pnl_snx
print()
print("★" * 65)
print("  SCENARIO A: FIXED +28% TARGET (current config)")
print(f"  NIFTY  : Rs{pnl_nf:+,.0f}  (exit ~10:05)")
print(f"  BNF    : Rs{pnl_bnf:+,.0f}  (exit ~10:20)")
print(f"  SENSEX : Rs{pnl_snx:+,.0f}  (exit ~10:05)")
print(f"  {'─'*30}")
print(f"  TOTAL  : Rs{total:+,.0f}")
print("★" * 65)

# Scenario B: Trail mode (checkpoint holds, trail exits)
print()
print("=" * 65)
print("  SCENARIO B: TRAIL MODE (checkpoint holds, trail exits)")
print("=" * 65)

tnf  = simulate_trail(nifty_bars, 24250, 0.22, 65, 12.0,  chk="10:55", name="NIFTY  24250 CE")
tbnf = simulate_trail(bnf_bars,   55900, 0.22, 30, 17.92, chk="10:55", name="BNF    55900 CE")
tsnx = simulate_trail(snx_bars,   77400, 0.15, 20, 18.25, chk="10:55", name="SENSEX 77400 CE")

ttotal = tnf + tbnf + tsnx
print()
print("=" * 65)
print("  SCENARIO B RESULT: Trail after checkpoint")
print(f"  NIFTY  : Rs{tnf:+,.0f}")
print(f"  BNF    : Rs{tbnf:+,.0f}")
print(f"  SENSEX : Rs{tsnx:+,.0f}")
print(f"  {'─'*30}")
print(f"  TOTAL  : Rs{ttotal:+,.0f}")
print("=" * 65)
print()
print("  DELTA (Trail vs Target):")
print(f"  If trail mode was used instead: Rs{ttotal - total:+,.0f}")
print("=" * 65)
