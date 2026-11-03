import { useCallback, useEffect, useMemo, useState } from 'react';
import { paperApi } from '../api/paper';
import { getParsedApiError } from '../api/error';
import type { ParsedApiError } from '../api/error';
import { ApiErrorAlert, Card, EmptyState } from '../components/common';
import type { PaperCurvePoint, PaperDashboard, PaperPosition } from '../types/paper';

// A 股惯例：涨用红色，跌用绿色
const UP_COLOR = 'var(--color-text-danger)';
const DOWN_COLOR = 'var(--color-text-success)';
const FLAT_COLOR = 'var(--color-text-secondary)';

function money(value?: number | null): string {
  if (value == null) return '--';
  return value.toLocaleString('zh-CN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function pctText(value?: number | null): string {
  if (value == null) return '--';
  return `${value > 0 ? '+' : ''}${value.toFixed(2)}%`;
}

function colorFor(value?: number | null): string {
  if (value == null) return FLAT_COLOR;
  if (value > 0) return UP_COLOR;
  if (value < 0) return DOWN_COLOR;
  return FLAT_COLOR;
}

function pnlPct(position: PaperPosition): number | null {
  const cost = Number(position.avgCost ?? 0);
  const price = Number(position.lastPrice ?? 0);
  if (!cost || !price) return null;
  return ((price - cost) / cost) * 100;
}

function StatCard({
  label,
  value,
  hint,
  tone,
}: {
  label: string;
  value: string;
  hint?: string;
  tone?: string;
}) {
  return (
    <div className="rounded-xl border border-[var(--color-border-tertiary)] px-4 py-3">
      <div className="text-xs text-[var(--color-text-tertiary)]">{label}</div>
      <div className="mt-1 text-lg font-semibold" style={{ color: tone || 'var(--color-text-primary)' }}>
        {value}
      </div>
      {hint ? (
        <div className="mt-1 text-xs text-[var(--color-text-tertiary)]">{hint}</div>
      ) : null}
    </div>
  );
}

function EquityCurve({ points }: { points: PaperCurvePoint[] }) {
  const geometry = useMemo(() => {
    if (points.length < 2) return null;
    const width = 640;
    const height = 140;
    const pad = 8;
    const values = points.map((item) => Number(item.totalEquity ?? 0));
    const min = Math.min(...values);
    const max = Math.max(...values);
    const span = max - min || 1;
    const step = (width - pad * 2) / (points.length - 1);
    const coords = values.map((value, index) => {
      const x = pad + index * step;
      const y = height - pad - ((value - min) / span) * (height - pad * 2);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    });
    return { width, height, coords: coords.join(' '), min, max };
  }, [points]);

  if (!geometry) {
    return (
      <p className="text-xs text-[var(--color-text-tertiary)]">
        净值数据不足（至少需要 2 个交易日）
      </p>
    );
  }

  const last = points[points.length - 1];
  return (
    <div>
      <svg
        viewBox={`0 0 ${geometry.width} ${geometry.height}`}
        className="h-36 w-full"
        preserveAspectRatio="none"
      >
        <polyline
          points={geometry.coords}
          fill="none"
          stroke="var(--color-text-primary)"
          strokeWidth="1.5"
        />
      </svg>
      <div className="mt-2 flex justify-between text-xs text-[var(--color-text-tertiary)]">
        <span>{points[0].date}</span>
        <span>
          最低 {money(geometry.min)}｜最高 {money(geometry.max)}
        </span>
        <span>{last.date}</span>
      </div>
    </div>
  );
}

function PositionRow({ position }: { position: PaperPosition }) {
  const pct = pnlPct(position);
  return (
    <div className="grid grid-cols-[1.4fr_repeat(4,1fr)] items-center gap-3 rounded-xl border border-[var(--color-border-tertiary)] px-4 py-2.5 text-sm">
      <div className="min-w-0">
        <div className="truncate font-medium text-[var(--color-text-primary)]">
          {position.symbol}
        </div>
      </div>
      <div className="text-right text-[var(--color-text-secondary)]">
        {Number(position.quantity ?? 0).toFixed(0)} 股
      </div>
      <div className="text-right text-[var(--color-text-secondary)]">
        {Number(position.avgCost ?? 0).toFixed(2)}
      </div>
      <div className="text-right text-[var(--color-text-secondary)]">
        {Number(position.lastPrice ?? 0).toFixed(2)}
      </div>
      <div className="text-right font-medium" style={{ color: colorFor(pct) }}>
        {pctText(pct)}
      </div>
    </div>
  );
}

export const PaperPage = () => {
  const [data, setData] = useState<PaperDashboard | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ParsedApiError | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const dashboard = await paperApi.getDashboard();
      setData(dashboard);
      setError(null);
    } catch (err) {
      setError(getParsedApiError(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (loading) {
    return (
      <div className="p-6 text-sm text-[var(--color-text-secondary)]">加载中…</div>
    );
  }

  if (error) {
    return (
      <div className="space-y-4 p-4">
        <ApiErrorAlert error={error} />
      </div>
    );
  }

  if (!data?.initialized) {
    return (
      <EmptyState
        title="模拟盘尚未初始化"
        description={data?.message || '先执行 --paper-init 创建模拟账户'}
      />
    );
  }

  const snapshot = data.snapshot || {};
  const positions = data.positions || [];
  const trades = data.trades || [];
  const risk = data.risk || {};

  return (
    <div className="space-y-4 p-4">
      <div>
        <h1 className="text-xl font-semibold text-[var(--color-text-primary)]">模拟盘</h1>
        <p className="mt-1 text-xs text-[var(--color-text-tertiary)]">
          数据截至 {data.asOf || '--'}｜初始资金 {money(data.initialCash)}
        </p>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard label="总资产" value={money(snapshot.totalEquity)} hint="元" />
        <StatCard
          label="累计收益"
          value={pctText(data.returnPct)}
          tone={colorFor(data.returnPct)}
        />
        <StatCard label="可用现金" value={money(snapshot.totalCash)} hint="元" />
        <StatCard
          label="持仓"
          value={`${positions.length} 只`}
          hint={`市值 ${money(snapshot.totalMarketValue)}`}
        />
      </div>

      <Card title="净值走势" subtitle="按交易日收盘估值">
        <EquityCurve points={data.curve || []} />
      </Card>

      <Card title="当前持仓">
        {positions.length ? (
          <div className="space-y-2">
            <div className="grid grid-cols-[1.4fr_repeat(4,1fr)] gap-3 px-4 text-xs text-[var(--color-text-tertiary)]">
              <span>标的</span>
              <span className="text-right">股数</span>
              <span className="text-right">成本</span>
              <span className="text-right">现价</span>
              <span className="text-right">浮盈</span>
            </div>
            {positions.map((position) => (
              <PositionRow key={position.symbol} position={position} />
            ))}
          </div>
        ) : (
          <p className="text-sm text-[var(--color-text-secondary)]">当前空仓</p>
        )}
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="近期成交">
          {trades.length ? (
            <div className="space-y-2">
              {trades.slice(0, 12).map((trade, index) => (
                <div
                  key={`${trade.symbol}-${trade.tradeDate}-${index}`}
                  className="flex items-center justify-between rounded-lg border border-[var(--color-border-tertiary)] px-3 py-2 text-sm"
                >
                  <span className="flex items-center gap-2">
                    <span
                      className="rounded px-1.5 py-0.5 text-xs"
                      style={{
                        color: trade.side === 'buy' ? UP_COLOR : DOWN_COLOR,
                      }}
                    >
                      {trade.side === 'buy' ? '买入' : '卖出'}
                    </span>
                    <span className="text-[var(--color-text-primary)]">{trade.symbol}</span>
                  </span>
                  <span className="text-xs text-[var(--color-text-tertiary)]">
                    {Number(trade.quantity ?? 0).toFixed(0)} 股 @
                    {Number(trade.price ?? 0).toFixed(2)}
                  </span>
                  <span className="max-w-[8rem] truncate text-xs text-[var(--color-text-tertiary)]">
                    {trade.note || '--'}
                  </span>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-sm text-[var(--color-text-secondary)]">暂无成交</p>
          )}
        </Card>

        <Card title="风控参数" subtitle="盘中按实时价触发">
          <ul className="space-y-2 text-sm text-[var(--color-text-secondary)]">
            <li>硬止损：{pctText(risk.stopLossPct)}</li>
            <li>硬止盈：{pctText(risk.takeProfitPct)}</li>
            <li>
              移动止盈：浮盈达 {pctText(risk.trailingActivatePct)} 后，从最高价回落{' '}
              {risk.trailingDrawdownPct != null
                ? `${Number(risk.trailingDrawdownPct).toFixed(1)}%`
                : '--'}
            </li>
            <li>盘中检查：10:00 / 11:00 / 13:00 / 14:00</li>
          </ul>
          <p className="mt-3 text-xs text-[var(--color-text-tertiary)]">
            模拟成交按实时价并计佣金与印花税，仅供研究参考。
          </p>
        </Card>
      </div>
    </div>
  );
};

export default PaperPage;
