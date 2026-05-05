//@version=6
strategy(title='TYN STRAT v28.1 — ORDER LAB WEBHOOK SAFE BTC10m', shorttitle='TYN STRAT v28.1 WEBHOOK SAFE', overlay=true, initial_capital=1000, currency=currency.USD, default_qty_type=strategy.percent_of_equity, default_qty_value=20, pyramiding=0, commission_type=strategy.commission.percent, commission_value=0.10, process_orders_on_close=true, margin_long=100, margin_short=100, max_labels_count=300, max_lines_count=300)

// =====================================================================
// TYN STRAT v28.1 — ORDER LAB WEBHOOK SAFE BTC10m
// Gebouwd op v25 Lock looser: 10m entries + confirmed 4h regime.
// Doel: kleine varianten rond de bijna break-even 1-jaar test; minder fee-drag en betere hold.
// Anti-repaint:
// - HTF-data gebruikt request.security(... expr[1], lookahead_off)
// - Entries alleen op barstate.isconfirmed
// - Breakouts gebruiken high[1] / low[1]
// =====================================================================

capital_ref = 1000.0

group_basis = '01 — Basis'
trade_mode = input.string('Long only', 'Trade mode', options=['Long only', 'Long & Short', 'Short only'], group=group_basis)
preset = input.string('V25 exact', 'Preset', options=['V25 exact', 'Loss pause 12h', 'Loss pause 24h', 'One HTF trade', 'Quality + pause', 'Later breakout + pause', 'Lock looser guarded', 'No regime exit guarded', 'Manual'], group=group_basis)
use_date_filter = input.bool(false, 'Gebruik backtest datumfilter', group=group_basis)
start_time = input.time(timestamp('2024-01-01T00:00:00'), 'Startdatum', group=group_basis)
end_time = input.time(timestamp('2030-01-01T00:00:00'), 'Einddatum', group=group_basis)
in_range = not use_date_filter or (time >= start_time and time <= end_time)

group_htf = '02 — HTF Regime filter'
htf_tf_in = input.timeframe('240', 'HTF timeframe', group=group_htf)
htf_fast_in = input.int(21, 'HTF fast EMA', minval=1, group=group_htf, inline='h1')
htf_slow_in = input.int(55, 'HTF slow EMA', minval=1, group=group_htf, inline='h1')
htf_trend_in = input.int(200, 'HTF trend EMA', minval=1, group=group_htf, inline='h2')
htf_slope_bars_in = input.int(2, 'HTF slope bars', minval=1, group=group_htf, inline='h2')
use_htf_filter = input.bool(true, 'Gebruik HTF-regimefilter', group=group_htf)

group_entry = '03 — 10m Entry engine'
fast_in = input.int(34, 'Fast EMA', minval=1, group=group_entry, inline='e1')
mid_in = input.int(89, 'Mid EMA', minval=1, group=group_entry, inline='e1')
slow_in = input.int(144, 'Slow EMA', minval=1, group=group_entry, inline='e1')
trend_in = input.int(200, 'Trend EMA', minval=1, group=group_entry, inline='e2')
adx_in = input.float(26.0, 'ADX min', minval=1, step=0.5, group=group_entry, inline='adx')
di_len = input.int(14, 'DI len', minval=1, group=group_entry, inline='adx')
adx_smooth = input.int(14, 'ADX smooth', minval=1, group=group_entry, inline='adx')
rsi_long_in = input.float(58.0, 'RSI min long', minval=1, maxval=99, step=0.5, group=group_entry, inline='rsi')
rsi_short_in = input.float(42.0, 'RSI max short', minval=1, maxval=99, step=0.5, group=group_entry, inline='rsi')
atr_pct_in = input.float(0.14, 'ATR% min', minval=0, step=0.01, group=group_entry, inline='chop')
spread_in = input.float(0.12, 'EMA spread% min', minval=0, step=0.01, group=group_entry, inline='chop')
breakout_len_in = input.int(60, 'Breakout lookback', minval=2, group=group_entry)
entry_style_in = input.string('Pullback + Breakout', 'Entry style', options=['Pullback + Breakout', 'Breakout only', 'EMA regime only'], group=group_entry)
cooldown_in = input.int(48, 'Cooldown bars na entry', minval=0, group=group_entry)
min_hold_in = input.int(24, 'Min hold bars vóór regime-exit', minval=0, group=group_entry)

group_exit = '04 — Exit / Risk'
atr_len = input.int(14, 'ATR lengte', minval=1, group=group_exit)
long_sl_in = input.float(3.0, 'Long SL ATR', minval=0.1, step=0.1, group=group_exit, inline='lr')
long_trail_in = input.float(4.0, 'Long trail ATR', minval=0.1, step=0.1, group=group_exit, inline='lr')
short_sl_in = input.float(2.7, 'Short SL ATR', minval=0.1, step=0.1, group=group_exit, inline='sr')
short_trail_in = input.float(3.5, 'Short trail ATR', minval=0.1, step=0.1, group=group_exit, inline='sr')
use_regime_exit_in = input.bool(true, 'Exit bij echte regimebreuk na min hold', group=group_exit)
use_profit_lock_in = input.bool(true, 'Gebruik break-even / profit-lock', group=group_exit)
lock_trigger_in = input.float(1.2, 'Lock trigger ATR', minval=0.1, step=0.1, group=group_exit, inline='lock')
lock_offset_in = input.float(0.15, 'Lock winst ATR', minval=0.0, step=0.05, group=group_exit, inline='lock')
long_tp_in = input.float(0.0, 'Long TP ATR, 0 = uit', minval=0.0, step=0.5, group=group_exit)

group_visual = '05 — Dashboard / Visuals'
show_dashboard = input.bool(true, 'Toon dashboard', group=group_visual)
show_emas = input.bool(true, 'Toon EMA lijnen', group=group_visual)
show_labels = input.bool(true, 'Toon entry labels', group=group_visual)

group_alerts = '06 — Webhook alerts'
webhook_symbol = input.string('BTCUSDT', 'Webhook symbol', group=group_alerts)
webhook_source = input.string('TYN_STRAT_v28_1', 'Webhook source', group=group_alerts)

make_msg(_action) =>
    '{"action":"' + _action + '","symbol":"' + webhook_symbol + '","price":' + str.tostring(close, format.mintick) + ',"source":"' + webhook_source + '","tf":"' + timeframe.period + '"}'

buy_msg = make_msg('buy')
sell_msg = make_msg('sell')

// Presets rond de bijna break-even v25 Lock looser 1-jaar test.
// Order-analyse: winnaars duren gemiddeld veel langer dan verliezers; verliezers komen vaak in clusters.
// Daarom testen we vooral pauzes na exits/verliestrades en minder re-entry binnen dezelfde HTF-regimefase.
is_auto = preset != 'Manual'
auto_htf_tf = is_auto ? '240' : htf_tf_in
auto_adx = preset == 'Quality + pause' or preset == 'Later breakout + pause' ? 28.0 : is_auto ? 26.0 : adx_in
auto_rsi_long = preset == 'Quality + pause' ? 60.0 : preset == 'Later breakout + pause' ? 59.0 : is_auto ? 58.0 : rsi_long_in
auto_rsi_short = is_auto ? 42.0 : rsi_short_in
auto_atr_pct = preset == 'Quality + pause' ? 0.16 : preset == 'Lock looser guarded' ? 0.15 : is_auto ? 0.14 : atr_pct_in
auto_spread = preset == 'Quality + pause' ? 0.14 : preset == 'Lock looser guarded' ? 0.13 : is_auto ? 0.12 : spread_in
auto_cooldown = preset == 'Later breakout + pause' or preset == 'Quality + pause' ? 72 : preset == 'One HTF trade' ? 48 : is_auto ? 36 : cooldown_in
auto_min_hold = preset == 'No regime exit guarded' ? 48 : is_auto ? 36 : min_hold_in
auto_breakout = preset == 'Later breakout + pause' ? 72 : is_auto ? 60 : breakout_len_in
auto_entry_style = preset == 'Later breakout + pause' ? 'Breakout only' : is_auto ? 'Pullback + Breakout' : entry_style_in
auto_use_regime_exit = preset == 'No regime exit guarded' ? false : use_regime_exit_in
auto_long_sl = is_auto ? 3.2 : long_sl_in
auto_long_trail = preset == 'No regime exit guarded' ? 5.5 : is_auto ? 5.0 : long_trail_in
auto_use_profit_lock = true
auto_lock_trigger = preset == 'Lock looser guarded' ? 1.8 : is_auto ? 1.6 : lock_trigger_in
auto_lock_offset = preset == 'Lock looser guarded' ? 0.35 : is_auto ? 0.25 : lock_offset_in
auto_long_tp = long_tp_in
// Extra guardrails na analyse van de orderlijst. Bars zijn 10m: 72 = 12h, 144 = 24h.
auto_exit_cooldown = preset == 'One HTF trade' ? 24 : preset == 'Lock looser guarded' ? 12 : 0
auto_loss_cooldown = preset == 'Loss pause 12h' or preset == 'Quality + pause' or preset == 'Later breakout + pause' or preset == 'Lock looser guarded' or preset == 'No regime exit guarded' ? 72 : preset == 'Loss pause 24h' ? 144 : 0
auto_one_per_htf = preset == 'One HTF trade'

// HTF confirmed regime, no repaint: [1] inside security call.
htf_close = request.security(syminfo.tickerid, auto_htf_tf, close[1], barmerge.gaps_off, barmerge.lookahead_off)
htf_fast = request.security(syminfo.tickerid, auto_htf_tf, ta.ema(close, htf_fast_in)[1], barmerge.gaps_off, barmerge.lookahead_off)
htf_slow = request.security(syminfo.tickerid, auto_htf_tf, ta.ema(close, htf_slow_in)[1], barmerge.gaps_off, barmerge.lookahead_off)
htf_trend = request.security(syminfo.tickerid, auto_htf_tf, ta.ema(close, htf_trend_in)[1], barmerge.gaps_off, barmerge.lookahead_off)
htf_trend_old = request.security(syminfo.tickerid, auto_htf_tf, ta.ema(close, htf_trend_in)[htf_slope_bars_in + 1], barmerge.gaps_off, barmerge.lookahead_off)
htf_time_confirmed = request.security(syminfo.tickerid, auto_htf_tf, time[1], barmerge.gaps_off, barmerge.lookahead_off)

htf_bull = not use_htf_filter or (htf_close > htf_trend and htf_fast > htf_slow and htf_trend > htf_trend_old)
htf_bear = not use_htf_filter or (htf_close < htf_trend and htf_fast < htf_slow and htf_trend < htf_trend_old)

// 10m indicators
ema_fast = ta.ema(close, fast_in)
ema_mid = ta.ema(close, mid_in)
ema_slow = ta.ema(close, slow_in)
ema_trend = ta.ema(close, trend_in)
rsi = ta.rsi(close, 14)
atr = ta.atr(atr_len)
atr_pct = atr / close * 100.0
[di_plus, di_minus, adx] = ta.dmi(di_len, adx_smooth)
ema_spread_pct = math.abs(ema_fast - ema_slow) / close * 100.0
not_chop = atr_pct >= auto_atr_pct and ema_spread_pct >= auto_spread

long_trend = close > ema_trend and ema_fast > ema_mid and ema_mid > ema_slow and di_plus > di_minus
short_trend = close < ema_trend and ema_fast < ema_mid and ema_mid < ema_slow and di_minus > di_plus
long_momentum = adx >= auto_adx and rsi >= auto_rsi_long
short_momentum = adx >= auto_adx and rsi <= auto_rsi_short
pullback_long = close > ema_mid and low <= ema_fast
pullback_short = close < ema_mid and high >= ema_fast
breakout_long = close > ta.highest(high[1], auto_breakout)
breakout_short = close < ta.lowest(low[1], auto_breakout)
entry_ok_long = auto_entry_style == 'EMA regime only' ? true : auto_entry_style == 'Breakout only' ? breakout_long : (pullback_long or breakout_long)
entry_ok_short = auto_entry_style == 'EMA regime only' ? true : auto_entry_style == 'Breakout only' ? breakout_short : (pullback_short or breakout_short)

var int last_entry_bar = na
var int last_exit_bar = na
var int last_loss_bar = na
var int last_closed_count = 0
var int last_entry_htf_time = na

// Update bij gesloten trade: hiermee kunnen we verliesclusters blokkeren zonder forward-looking.
if strategy.closedtrades > last_closed_count
    last_profit = strategy.closedtrades.profit(strategy.closedtrades - 1)
    last_exit_bar := bar_index
    if last_profit < 0
        last_loss_bar := bar_index
    last_closed_count := strategy.closedtrades

entry_cooldown_ok = na(last_entry_bar) or (bar_index - last_entry_bar >= auto_cooldown)
exit_cooldown_ok = na(last_exit_bar) or (bar_index - last_exit_bar >= auto_exit_cooldown)
loss_cooldown_ok = na(last_loss_bar) or (bar_index - last_loss_bar >= auto_loss_cooldown)
htf_reentry_ok = not auto_one_per_htf or na(last_entry_htf_time) or htf_time_confirmed != last_entry_htf_time
cooldown_ok = entry_cooldown_ok and exit_cooldown_ok and loss_cooldown_ok and htf_reentry_ok
allow_long = trade_mode == 'Long only' or trade_mode == 'Long & Short'
allow_short = trade_mode == 'Short only' or trade_mode == 'Long & Short'
long_signal = barstate.isconfirmed and in_range and cooldown_ok and allow_long and htf_bull and long_trend and long_momentum and not_chop and entry_ok_long
short_signal = barstate.isconfirmed and in_range and cooldown_ok and allow_short and htf_bear and short_trend and short_momentum and not_chop and entry_ok_short

if long_signal and strategy.position_size <= 0
    strategy.entry('LONG', strategy.long, comment='Long HTF Lock', alert_message=buy_msg)
    last_entry_bar := bar_index
    last_entry_htf_time := htf_time_confirmed
    if show_labels
        label.new(bar_index, low, 'LONG', style=label.style_label_up, textcolor=color.white, color=color.new(color.green, 0), size=size.tiny)

if short_signal and strategy.position_size >= 0
    strategy.entry('SHORT', strategy.short, comment='Short HTF Lock', alert_message=sell_msg)
    last_entry_bar := bar_index
    last_entry_htf_time := htf_time_confirmed
    if show_labels
        label.new(bar_index, high, 'SHORT', style=label.style_label_down, textcolor=color.white, color=color.new(color.red, 0), size=size.tiny)

// Position tracking
var float long_hh = na
var float short_ll = na
var int pos_entry_bar = na
new_pos = strategy.position_size != 0 and strategy.position_size[1] == 0
flat_now = strategy.position_size == 0
if new_pos
    pos_entry_bar := bar_index
    long_hh := high
    short_ll := low
if flat_now
    pos_entry_bar := na
    long_hh := na
    short_ll := na
hold_bars = na(pos_entry_bar) ? 0 : bar_index - pos_entry_bar
can_regime_exit = hold_bars >= auto_min_hold

if strategy.position_size > 0
    long_hh := na(long_hh) ? high : math.max(long_hh, high)
    long_initial_stop = strategy.position_avg_price - auto_long_sl * atr
    long_trailing_stop = long_hh - auto_long_trail * atr
    long_stop = math.max(long_initial_stop, long_trailing_stop)
    long_lock_stop = auto_use_profit_lock and long_hh >= strategy.position_avg_price + auto_lock_trigger * atr ? strategy.position_avg_price + auto_lock_offset * atr : na
    long_final_stop = na(long_lock_stop) ? long_stop : math.max(long_stop, long_lock_stop)
    long_limit = auto_long_tp > 0 ? strategy.position_avg_price + auto_long_tp * atr : na
    strategy.exit('LONG EXIT', 'LONG', stop=long_final_stop, limit=long_limit, comment='Long ATR/Trail/Lock', alert_message=sell_msg)
    if auto_use_regime_exit and can_regime_exit and barstate.isconfirmed and (close < ema_slow or not htf_bull)
        strategy.close('LONG', comment='Long regime break', alert_message=sell_msg)

if strategy.position_size < 0
    short_ll := na(short_ll) ? low : math.min(short_ll, low)
    short_initial_stop = strategy.position_avg_price + short_sl_in * atr
    short_trailing_stop = short_ll + short_trail_in * atr
    short_stop = math.min(short_initial_stop, short_trailing_stop)
    strategy.exit('SHORT EXIT', 'SHORT', stop=short_stop, comment='Short ATR/Trail', alert_message=buy_msg)
    if auto_use_regime_exit and can_regime_exit and barstate.isconfirmed and (close > ema_slow or not htf_bear)
        strategy.close('SHORT', comment='Short regime break', alert_message=buy_msg)

plot(show_emas ? ema_fast : na, 'EMA fast', color=color.new(color.orange, 0))
plot(show_emas ? ema_mid : na, 'EMA mid', color=color.new(color.red, 0))
plot(show_emas ? ema_slow : na, 'EMA slow', color=color.new(color.white, 0), linewidth=2)
plot(show_emas ? ema_trend : na, 'EMA trend', color=color.new(color.blue, 0), linewidth=2)
bgcolor(htf_bull ? color.new(color.green, 92) : htf_bear ? color.new(color.red, 92) : na)

// Dashboard
var float bh_start = na
if in_range and na(bh_start)
    bh_start := close
bh_pct = na(bh_start) ? na : (close / bh_start - 1.0) * 100.0
net_pct = strategy.netprofit / capital_ref * 100.0
dd_pct = strategy.max_drawdown / capital_ref * 100.0
pf = strategy.grossloss != 0 ? strategy.grossprofit / math.abs(strategy.grossloss) : na
winrate = strategy.closedtrades > 0 ? strategy.wintrades / strategy.closedtrades * 100.0 : na
edge_vs_bh = na(bh_pct) ? na : net_pct - bh_pct

var table dash = table.new(position.top_left, 2, 12, border_width=1)
if show_dashboard and barstate.islast
    table.cell(dash, 0, 0, 'TYN STRAT v28.1', text_color=color.white, bgcolor=color.new(color.blue, 40))
    table.cell(dash, 1, 0, preset, text_color=color.white, bgcolor=color.new(color.blue, 40))
    table.cell(dash, 0, 1, 'Mode / TF')
    table.cell(dash, 1, 1, trade_mode + ' / HTF ' + auto_htf_tf)
    table.cell(dash, 0, 2, 'Net %')
    table.cell(dash, 1, 2, str.tostring(net_pct, '#.##') + '%', text_color=net_pct >= 0 ? color.lime : color.red)
    table.cell(dash, 0, 3, 'Max DD %')
    table.cell(dash, 1, 3, str.tostring(dd_pct, '#.##') + '%')
    table.cell(dash, 0, 4, 'Profit factor')
    table.cell(dash, 1, 4, str.tostring(pf, '#.###'), text_color=pf >= 1 ? color.lime : color.red)
    table.cell(dash, 0, 5, 'Trades / Win%')
    table.cell(dash, 1, 5, str.tostring(strategy.closedtrades) + ' / ' + str.tostring(winrate, '#.##') + '%')
    table.cell(dash, 0, 6, 'Buy&Hold %')
    table.cell(dash, 1, 6, str.tostring(bh_pct, '#.##') + '%')
    table.cell(dash, 0, 7, 'Edge vs B&H')
    table.cell(dash, 1, 7, str.tostring(edge_vs_bh, '#.##') + '%', text_color=edge_vs_bh >= 0 ? color.lime : color.orange)
    table.cell(dash, 0, 8, 'ADX / ATR% / Spread%')
    table.cell(dash, 1, 8, str.tostring(auto_adx, '#.#') + ' / ' + str.tostring(auto_atr_pct, '#.##') + ' / ' + str.tostring(auto_spread, '#.##'))
    table.cell(dash, 0, 9, 'RSI L/S')
    table.cell(dash, 1, 9, str.tostring(auto_rsi_long, '#.#') + ' / ' + str.tostring(auto_rsi_short, '#.#'))
    table.cell(dash, 0, 10, 'SL / Trail / Lock')
    table.cell(dash, 1, 10, str.tostring(auto_long_sl, '#.#') + ' / ' + str.tostring(auto_long_trail, '#.#') + ' / ' + (auto_use_profit_lock ? str.tostring(auto_lock_trigger, '#.#') + '→' + str.tostring(auto_lock_offset, '#.##') : 'uit'))
    table.cell(dash, 0, 11, 'Exit/Loss pause')
    table.cell(dash, 1, 11, str.tostring(auto_exit_cooldown) + ' / ' + str.tostring(auto_loss_cooldown) + ' bars', text_color=color.lime)
