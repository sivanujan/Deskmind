"""Report generators for backtest runs.

Outputs:
1. Markdown report (report.md)
2. Interactive self-contained HTML report (index.html) with equity curve, drawdown,
   R distribution, monthly table
3. Trades CSV (trades.csv)
Saved to reports/backtests/{run_id}/
"""

from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path

from deskmind.backtest.baselines import BuyAndHoldResult, RandomBaselineResult
from deskmind.backtest.engine import BacktestResult
from deskmind.backtest.metrics import PerformanceMetrics


def generate_reports(
    run_id: str,
    strategy_name: str,
    strategy_version: str,
    split: str,
    symbols: list[str],
    metrics: PerformanceMetrics,
    result: BacktestResult,
    bnh: dict[str, BuyAndHoldResult],
    random_base: RandomBaselineResult,
    output_dir: Path,
) -> Path:
    run_dir = output_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # 1. Trades CSV
    csv_path = run_dir / "trades.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "id",
                "symbol",
                "side",
                "entry_time_utc",
                "entry_price",
                "exit_time_utc",
                "exit_price",
                "qty",
                "pnl",
                "pnl_pct",
                "r_multiple",
                "exit_reason",
                "fees_paid",
                "funding_paid",
                "slippage_paid",
                "holding_time_min",
                "spanned_gap",
                "initial_risk",
            ]
        )
        for t in result.trades:
            e_time = datetime.fromtimestamp(t.entry_time_ms / 1000, UTC).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            x_time = datetime.fromtimestamp(t.exit_time_ms / 1000, UTC).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            writer.writerow(
                [
                    t.id,
                    t.symbol,
                    t.side,
                    e_time,
                    f"{t.entry_price:.4f}",
                    x_time,
                    f"{t.exit_price:.4f}",
                    f"{t.qty:.4f}",
                    f"{t.pnl:.2f}",
                    f"{t.pnl_pct:.2f}",
                    f"{t.r_multiple:.2f}",
                    t.exit_reason,
                    f"{t.fees_paid:.4f}",
                    f"{t.funding_paid:.4f}",
                    f"{t.slippage_paid:.4f}",
                    t.holding_time_minutes,
                    t.spanned_gap,
                    f"{t.initial_risk:.2f}",
                ]
            )

    # 2. Markdown Report
    md_path = run_dir / "report.md"
    start_dt = datetime.fromtimestamp(result.start_time_ms / 1000, UTC).strftime("%Y-%m-%d")
    end_dt = datetime.fromtimestamp(result.end_time_ms / 1000, UTC).strftime("%Y-%m-%d")
    h_time = metrics.avg_holding_time_minutes

    lines = [
        f"# Backtest Report: {strategy_name} (v{strategy_version})",
        f"**Run ID**: `{run_id}`  ",
        f"**Split**: `{split}` ({start_dt} to {end_dt})  ",
        f"**Symbols**: {', '.join(symbols)}  ",
        f"**Generated**: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}  ",
        "",
        "## 1. Key Performance Metrics",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Initial Cash | ${metrics.initial_cash:,.2f} |",
        f"| Final Equity | ${metrics.final_equity:,.2f} |",
        f"| Net Return | ${metrics.net_return_usd:+,.2f} ({metrics.net_return_pct:+.2f}%) |",
        f"| CAGR | {metrics.cagr_pct:+.2f}% |",
        f"| Max Drawdown | ${metrics.max_drawdown_usd:,.2f} ({metrics.max_drawdown_pct:.2f}%) |",
        f"| Max Drawdown Duration | {metrics.max_drawdown_duration_days:.1f} days |",
        f"| Sharpe Ratio | {metrics.sharpe_ratio:.2f} |",
        f"| Sortino Ratio | {metrics.sortino_ratio:.2f} |",
        f"| Profit Factor | {metrics.profit_factor:.2f} |",
        f"| Total Trades | {metrics.total_trades} |",
        (
            f"| Win Rate | {metrics.win_rate_pct:.1f}% "
            f"({metrics.winning_trades}W / {metrics.losing_trades}L) |"
        ),
        f"| Average R | {metrics.avg_r:+.2f}R |",
        f"| Expectancy | {metrics.expectancy_r:+.2f}R ({metrics.expectancy_pct:+.2f}%) |",
        f"| Gross Expectancy | {metrics.gross_expectancy_r:+.2f}R |",
        f"| Avg Cost per Trade | {metrics.avg_cost_per_trade_r:.2f}R |",
        f"| Avg Holding Time | {h_time:.1f} min ({h_time / 60:.1f}h) |",
        f"| Market Exposure | {metrics.exposure_time_pct:.2f}% of time |",
        f"| Total Fees Paid | ${metrics.total_fees_paid:,.2f} |",
        f"| Total Funding Paid | ${metrics.total_funding_paid:,.2f} |",
        f"| Total Slippage Paid | ${metrics.total_slippage_paid:,.2f} |",
        f"| Liquidations | {metrics.liquidations_count} |",
        f"| Same-Bar SL+TP Hits | {metrics.same_bar_sl_tp_count} (assumed SL first) |",
        f"| Longest Losing Streak | {metrics.longest_losing_streak} trades |",
        f"| Skipped Concurrent Signals | {metrics.skipped_signals_count} |",
        "",
        "## 2. Cost Breakdown",
        "",
        "| Component | Amount | Impact / Accounting |",
        "|---|---|---|",
        (
            f"| Gross PnL (before costs) | ${metrics.gross_pnl_usd:+,.2f} | "
            "Strategy trading edge before friction |"
        ),
        (
            f"| Maker/Taker Fees | -${metrics.total_fees_paid:,.2f} | "
            "Binance VIP0 rates (0.02% maker / 0.05% taker) |"
        ),
        (f"| Execution Slippage | -${metrics.total_slippage_paid:,.2f} | 2 bps + 0.05 ATR model |"),
        (
            f"| Historical Funding | -${metrics.total_funding_paid:,.2f} | "
            "Real historical funding applied at event timestamps |"
        ),
        (
            f"| **Total Costs (Friction)** | **-${metrics.total_costs_usd:,.2f}** | "
            "Sum of fees, slippage, and funding |"
        ),
        f"| **Net PnL (after costs)** | **${metrics.net_return_usd:+,.2f}** | Final net return |",
        (
            f"| **Cost as % of Gross PnL** | **{metrics.cost_as_pct_of_gross:.1f}%** | "
            "Total friction relative to gross magnitude |"
        ),
        (
            f"| **Average Cost per Trade** | **{metrics.avg_cost_per_trade_r:.2f}R** | "
            "Average friction drag per trade in R units |"
        ),
        (
            f"| **Gross Expectancy** | **{metrics.gross_expectancy_r:+.2f}R** | "
            "Expectancy per trade before friction costs |"
        ),
        "",
        "## 3. Benchmark & Baseline Comparison",
        "",
        "### A. Random-Entry Baseline",
        (
            f"- **Strategy Rank**: **{random_base.percentile:.1f}th percentile** "
            f"across {random_base.num_seeds} random seeds."
        ),
        f"- **Beat Count**: Beat {random_base.beat_count} / {random_base.num_seeds} iterations.",
        f"- **Random Median Return**: ${random_base.median_random_return_usd:+,.2f}",
        (
            f"- **Random Range**: [${random_base.min_random_return_usd:+,.2f}, "
            f"${random_base.max_random_return_usd:+,.2f}]"
        ),
        (
            "- **Verdict**: **"
            + (
                "BEATS random entry benchmark"
                if random_base.percentile >= 50
                else "DOES NOT BEAT random entry benchmark"
            )
            + "**"
        ),
        (
            f"- **95% Confidence Interval**: "
            f"[{random_base.ci_lower:.1f}%, {random_base.ci_upper:.1f}%]"
            if random_base.ci_upper > 0
            else ""
        ),
        (
            f"- **Confidence Note**: {random_base.confidence_note}"
            if random_base.confidence_note
            else ""
        ),
        "",
        "### B. Buy & Hold per Symbol",
        "",
        "| Symbol | Start Price | End Price | Price Return | Net Return | Fees | Funding |",
        "|---|---|---|---|---|---|---|",
    ]

    for s, b in bnh.items():
        lines.append(
            f"| {s} | ${b.start_price:,.2f} | ${b.end_price:,.2f} | {b.price_return_pct:+.2f}% | "
            f"{b.net_return_pct:+.2f}% | ${b.fees_paid_usd:,.2f} | ${b.funding_paid_usd:,.2f} |"
        )

    lines.extend(
        [
            "",
            "## 3. Per-Symbol Trade Breakdown",
            "",
            "| Symbol | Trades | Win Rate | Net PnL | Profit Factor | Avg R | Fees | Funding |",
            "|---|---|---|---|---|---|---|---|",
        ]
    )

    for s, data in metrics.per_symbol.items():
        lines.append(
            f"| {s} | {data['trades']} | {data['win_rate_pct']:.1f}% | "
            f"${data['net_pnl_usd']:+,.2f} | {data['profit_factor']:.2f} | "
            f"{data['avg_r']:+.2f}R | ${data['fees_paid']:,.2f} | ${data['funding_paid']:,.2f} |"
        )

    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    # 3. Interactive Self-Contained HTML Report
    html_path = run_dir / "index.html"
    _write_html_report(
        html_path,
        run_id,
        strategy_name,
        strategy_version,
        split,
        symbols,
        metrics,
        result,
        bnh,
        random_base,
    )

    return run_dir


def _write_html_report(
    html_path: Path,
    run_id: str,
    strategy_name: str,
    strategy_version: str,
    split: str,
    symbols: list[str],
    metrics: PerformanceMetrics,
    result: BacktestResult,
    bnh: dict[str, BuyAndHoldResult],
    random_base: RandomBaselineResult,
) -> None:
    monthly_data: dict[str, dict[str, float]] = {}
    months_labels = [
        "Jan",
        "Feb",
        "Mar",
        "Apr",
        "May",
        "Jun",
        "Jul",
        "Aug",
        "Sep",
        "Oct",
        "Nov",
        "Dec",
    ]

    daily_equities: dict[str, float] = {}
    for pt in result.equity_curve:
        d = datetime.fromtimestamp(pt.time_ms / 1000, UTC).strftime("%Y-%m-%d")
        daily_equities[d] = float(pt.equity)

    sorted_days = sorted(daily_equities.keys())
    if sorted_days:
        for day in sorted_days:
            y = day[:4]
            m_idx = int(day[5:7]) - 1
            m_name = months_labels[m_idx]
            if y not in monthly_data:
                monthly_data[y] = {m: 0.0 for m in months_labels}
                monthly_data[y]["Total"] = 0.0

    eq_by_month: dict[str, list[float]] = {}
    for pt in result.equity_curve:
        ym = datetime.fromtimestamp(pt.time_ms / 1000, UTC).strftime("%Y-%m")
        eq_by_month.setdefault(ym, []).append(float(pt.equity))

    prev_close = float(result.initial_cash)
    for ym, series in sorted(eq_by_month.items()):
        y = ym[:4]
        m_idx = int(ym[5:7]) - 1
        m_name = months_labels[m_idx]
        last_val = series[-1]
        m_ret = ((last_val - prev_close) / prev_close) * 100.0 if prev_close > 0 else 0.0
        if y in monthly_data:
            monthly_data[y][m_name] = m_ret
        prev_close = last_val

    for y in monthly_data:
        m_vals = [monthly_data[y][m] for m in months_labels if monthly_data[y][m] != 0.0]
        comp = 1.0
        for v in m_vals:
            comp *= 1.0 + v / 100.0
        monthly_data[y]["Total"] = (comp - 1.0) * 100.0

    pts = result.equity_curve
    step = max(1, len(pts) // 200)
    sample_pts = pts[::step]
    if pts and sample_pts[-1] != pts[-1]:
        sample_pts.append(pts[-1])

    min_eq = min((float(p.equity) for p in sample_pts), default=float(result.initial_cash))
    max_eq = max((float(p.equity) for p in sample_pts), default=float(result.initial_cash))
    eq_range = max(1.0, max_eq - min_eq)

    w, h = 800, 240
    svg_points = []
    for i, p in enumerate(sample_pts):
        x = (i / max(1, len(sample_pts) - 1)) * w
        y = h - ((float(p.equity) - min_eq) / eq_range) * (h - 20) - 10
        svg_points.append(f"{x:.1f},{y:.1f}")
    polyline_str = " ".join(svg_points)

    def td_month(y: str, m: str) -> str:
        val = monthly_data[y][m]
        cls = "positive" if val > 0 else ("negative" if val < 0 else "")
        text = f"{val:+.2f}%" if val != 0.0 else "-"
        return f"<td class='{cls}'>{text}</td>"

    rows_html = []
    for y in sorted(monthly_data.keys()):
        cols = [f"<td><b>{y}</b></td>"]
        for m in months_labels:
            cols.append(td_month(y, m))
        tot = monthly_data[y]["Total"]
        tot_cls = "positive" if tot > 0 else ("negative" if tot < 0 else "")
        cols.append(f"<td class='{tot_cls}'><b>{tot:+.2f}%</b></td>")
        rows_html.append(f"<tr>{''.join(cols)}</tr>")

    symbols_html = []
    for s, d in metrics.per_symbol.items():
        pnl_cls = "positive" if d["net_pnl_usd"] >= 0 else "negative"
        symbols_html.append(
            f"<tr><td><b>{s}</b></td><td>{d['trades']}</td><td>{d['win_rate_pct']:.1f}%</td>"
            f"<td class='{pnl_cls}'>${d['net_pnl_usd']:+,.2f}</td>"
            f"<td>{d['profit_factor']:.2f}</td><td>{d['avg_r']:+.2f}R</td>"
            f"<td>${d['fees_paid']:,.2f}</td><td>${d['funding_paid']:,.2f}</td></tr>"
        )

    b_pass = "badge-pass" if random_base.percentile >= 50 else "badge-warn"
    b_txt = "BEATS RANDOM ENTRY" if random_base.percentile >= 50 else "DOES NOT BEAT RANDOM ENTRY"
    gross_color = "#22c55e" if metrics.gross_pnl_usd >= 0 else "#ef4444"
    net_color = "#22c55e" if metrics.net_return_usd >= 0 else "#ef4444"
    fund_color = "#ef4444" if metrics.total_funding_paid >= 0 else "#22c55e"

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Backtest Report - {strategy_name} ({run_id})</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         background: #0f172a; color: #f8fafc; margin: 0; padding: 24px; }}
  .container {{ max-width: 1080px; margin: 0 auto; }}
  h1, h2, h3 {{ color: #38bdf8; font-weight: 600; margin-top: 24px; }}
  .card {{ background: #1e293b; border-radius: 8px; padding: 20px;
          margin-bottom: 20px; border: 1px solid #334155; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
          gap: 16px; margin-bottom: 20px; }}
  .stat-box {{ background: #0f172a; padding: 16px; border-radius: 6px; border: 1px solid #334155; }}
  .stat-label {{ font-size: 12px; color: #94a3b8; text-transform: uppercase; margin-bottom: 4px; }}
  .stat-value {{ font-size: 22px; font-weight: bold; color: #f8fafc; }}
  .positive {{ color: #4ade80 !important; }}
  .negative {{ color: #f87171 !important; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 14px; }}
  th, td {{ padding: 10px 12px; text-align: left; border-bottom: 1px solid #334155; }}
  th {{ background: #0f172a; color: #94a3b8; font-weight: 600; }}
  tr:hover {{ background: #243247; }}
  .chart-box {{ width: 100%; height: 260px; background: #0f172a; border-radius: 6px;
                border: 1px solid #334155; display: flex; align-items: center;
                justify-content: center; overflow: hidden; }}
  .badge {{ display: inline-block; padding: 4px 8px; border-radius: 4px;
            font-size: 12px; font-weight: 600; }}
  .badge-pass {{ background: #14532d; color: #4ade80; border: 1px solid #22c55e; }}
  .badge-warn {{ background: #713f12; color: #fde047; border: 1px solid #eab308; }}
</style>
</head>
<body>
<div class="container">
  <h1>Backtest Audit Report: {strategy_name} (v{strategy_version})</h1>
  <div style="color: #94a3b8; margin-bottom: 20px;">
    Run ID: <code>{run_id}</code> | Split: <b>{split}</b> | Symbols: {", ".join(symbols)}
  </div>

  <div class="grid">
    <div class="stat-box">
      <div class="stat-label">Net Return</div>
      <div class="stat-value {"positive" if metrics.net_return_usd >= 0 else "negative"}">
        ${metrics.net_return_usd:+,.2f} ({metrics.net_return_pct:+.2f}%)
      </div>
    </div>
    <div class="stat-box">
      <div class="stat-label">Profit Factor</div>
      <div class="stat-value">{metrics.profit_factor:.2f}</div>
    </div>
    <div class="stat-box">
      <div class="stat-label">Sharpe Ratio</div>
      <div class="stat-value">{metrics.sharpe_ratio:.2f}</div>
    </div>
    <div class="stat-box">
      <div class="stat-label">Max Drawdown</div>
      <div class="stat-value negative">
        -{metrics.max_drawdown_pct:.2f}% (${metrics.max_drawdown_usd:,.2f})
      </div>
    </div>
    <div class="stat-box">
      <div class="stat-label">Win Rate</div>
      <div class="stat-value">{metrics.win_rate_pct:.1f}% ({metrics.total_trades} trades)</div>
    </div>
    <div class="stat-box">
      <div class="stat-label">Average R</div>
      <div class="stat-value {"positive" if metrics.avg_r >= 0 else "negative"}">
        {metrics.avg_r:+.2f}R
      </div>
    </div>
    <div class="stat-box">
      <div class="stat-label">Random Rank</div>
      <div class="stat-value">{random_base.percentile:.1f}th pct</div>
    </div>
    <div class="stat-box">
      <div class="stat-label">Exposure Time</div>
      <div class="stat-value">{metrics.exposure_time_pct:.2f}%</div>
    </div>
  </div>

  <div class="card">
    <h2>Equity Curve (${min_eq:,.0f} &rarr; ${max_eq:,.0f})</h2>
    <div class="chart-box">
      <svg width="100%" height="240" viewBox="0 0 {w} {h}" preserveAspectRatio="none">
        <polyline fill="none" stroke="#38bdf8" stroke-width="2" points="{polyline_str}" />
      </svg>
    </div>
  </div>

  <div class="card">
    <h2>Monthly Returns (%)</h2>
    <table>
      <thead>
        <tr>
          <th>Year</th>
          {"".join(f"<th>{m}</th>" for m in months_labels)}
          <th>Year Total</th>
        </tr>
      </thead>
      <tbody>
        {"".join(rows_html)}
      </tbody>
    </table>
  </div>

  <div class="card">
    <h2>Random-Entry Baseline Benchmark</h2>
    <p>
      Ran <b>{random_base.num_seeds} random-entry iterations</b> with matching trade count
      ({metrics.total_trades}), direction mix, stop-loss, and 2R take-profit logic.
    </p>
    <p>
      Strategy Percentile: <b>{random_base.percentile:.1f}%</b>
      (Beat {random_base.beat_count} / {random_base.num_seeds} seeds).<br>
      Median Random Return: <b>${random_base.median_random_return_usd:+,.2f}</b><br>
      Status: <span class="badge {b_pass}">{b_txt}</span><br>
      <small style="color: #94a3b8">
        95% CI: [{random_base.ci_lower:.1f}%, {random_base.ci_upper:.1f}%] |
        {random_base.confidence_note}
      </small>
    </p>
  </div>

  <div class="card">
    <h2>Cost Breakdown & Friction Impact</h2>
    <table>
      <tr>
        <td>Gross PnL (before costs)</td>
        <td><b style="color: {gross_color}">${metrics.gross_pnl_usd:+,.2f}</b></td>
      </tr>
      <tr>
        <td>Maker & Taker Fees</td>
        <td><b style="color: #ef4444">-${metrics.total_fees_paid:,.2f}</b></td>
      </tr>
      <tr>
        <td>Execution Slippage</td>
        <td><b style="color: #ef4444">-${metrics.total_slippage_paid:,.2f}</b></td>
      </tr>
      <tr>
        <td>Historical Funding</td>
        <td><b style="color: {fund_color}">-${metrics.total_funding_paid:,.2f}</b></td>
      </tr>
      <tr>
        <td>Total Friction Costs</td>
        <td><b style="color: #ef4444">-${metrics.total_costs_usd:,.2f}</b></td>
      </tr>
      <tr>
        <td>Net PnL (after costs)</td>
        <td><b style="color: {net_color}">${metrics.net_return_usd:+,.2f}</b></td>
      </tr>
      <tr>
        <td>Cost as % of Gross PnL</td>
        <td><b style="color: #f59e0b">{metrics.cost_as_pct_of_gross:.1f}%</b></td>
      </tr>
      <tr>
        <td>Average Cost per Trade (R)</td>
        <td><b style="color: #ef4444">{metrics.avg_cost_per_trade_r:.2f}R</b></td>
      </tr>
      <tr>
        <td>Gross Expectancy (R)</td>
        <td><b style="color: {gross_color}">{metrics.gross_expectancy_r:+.2f}R</b></td>
      </tr>
    </table>
  </div>

  <div class="card">
    <h2>Per-Symbol Breakdown</h2>
    <table>
      <thead>
        <tr>
          <th>Symbol</th><th>Trades</th><th>Win Rate</th><th>Net PnL</th>
          <th>Profit Factor</th><th>Avg R</th><th>Fees</th><th>Funding</th>
        </tr>
      </thead>
      <tbody>
        {"".join(symbols_html)}
      </tbody>
    </table>
  </div>

  <div class="card">
    <h2>Execution Realism & Safety Accounting</h2>
    <table>
      <tr><td>Liquidations Count</td><td><b>{metrics.liquidations_count}</b></td></tr>
      <tr><td>Same-Bar SL & TP</td><td><b>{metrics.same_bar_sl_tp_count}</b> (SL assumed first)</td>
      </tr>
      <tr><td>Skipped Concurrent Signals</td><td><b>{metrics.skipped_signals_count}</b></td></tr>
      <tr><td>Total Fees Paid</td><td><b>${metrics.total_fees_paid:,.2f}</b></td></tr>
      <tr><td>Total Funding Paid</td><td><b>${metrics.total_funding_paid:,.2f}</b></td></tr>
      <tr><td>Total Slippage Paid</td><td><b>${metrics.total_slippage_paid:,.2f}</b></td></tr>
      <tr><td>Longest Losing Streak</td><td><b>{metrics.longest_losing_streak}</b> trades</td></tr>
    </table>
  </div>
</div>
</body>
</html>
"""
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)
