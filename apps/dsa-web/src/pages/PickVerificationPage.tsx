import { useCallback, useEffect, useMemo, useState } from 'react';
import { pickVerificationApi } from '../api/pickVerification';
import { getParsedApiError } from '../api/error';
import {
  ApiErrorAlert,
  Card,
  EmptyState,
  StatusDot,
} from '../components/common';
import type {
  PickDayGroup,
  PickVerificationDashboard,
  ScreeningPickItem,
} from '../types/pickVerification';

// A 股惯例：涨用红色，跌用绿色
const UP_COLOR = 'var(--color-text-danger)';
const DOWN_COLOR = 'var(--color-text-success)';
const FLAT_COLOR = 'var(--color-text-secondary)';

const MARKET_LABELS: Record<string, string> = {
  cn: 'A 股',
  us: '美股',
  hk: '港股',
};

function pctText(value?: number | null): string {
  if (value == null) return '--';
  const sign = value > 0 ? '+' : '';
  return `${sign}${value.toFixed(2)}%`;
}

function colorFor(value?: number | null): string {
  if (value == null) return FLAT_COLOR;
  if (value > 0) return UP_COLOR;
  if (value < 0) return DOWN_COLOR;
  return FLAT_COLOR;
}

function marketLabel(market: string): string {
  return MARKET_LABELS[market] || market.toUpperCase();
}

function statusLabel(verification: ScreeningPickItem['verification']): string {
  if (!verification) return '待验证';
  if (verification.evalStatus === 'error') return '取价失败';
  if (verification.evalStatus !== 'done') return '数据不足';
  if (verification.metExpectation) return '符合预期';
  if (!verification.isUp) return '未上涨';
  return '跑输基准';
}

function statusTone(verification: ScreeningPickItem['verification']): string {
  if (!verification || verification.evalStatus !== 'done') {
    return FLAT_COLOR;
  }
  return verification.metExpectation ? UP_COLOR : DOWN_COLOR;
}

function PickRow({ pick }: { pick: ScreeningPickItem }) {
  const verification = pick.verification;
  return (
    <div className="grid grid-cols-[2.2rem_1fr] items-center gap-3 rounded-xl border border-[var(--color-border-tertiary)] px-4 py-3 sm:grid-cols-[2.2rem_1.6fr_repeat(4,1fr)]">
      <span className="text-sm font-medium text-[var(--color-text-tertiary)]">
        #{pick.rank}
      </span>
      <div className="min-w-0">
        <div className="truncate text-sm font-medium text-[var(--color-text-primary)]">
          {pick.name || pick.code}
        </div>
        <div className="truncate text-xs text-[var(--color-text-tertiary)]">
          {pick.code}
        </div>
      </div>

      <div className="hidden text-right sm:block">
        <div className="text-xs text-[var(--color-text-tertiary)]">入场价</div>
        <div className="text-sm text-[var(--color-text-primary)]">
          {pick.entryPrice != null ? pick.entryPrice.toFixed(2) : '--'}
        </div>
      </div>

      <div className="hidden text-right sm:block">
        <div className="text-xs text-[var(--color-text-tertiary)]">次日收盘</div>
        <div className="text-sm text-[var(--color-text-primary)]">
          {verification?.closePrice != null
            ? verification.closePrice.toFixed(2)
            : '--'}
        </div>
      </div>

      <div className="hidden text-right sm:block">
        <div className="text-xs text-[var(--color-text-tertiary)]">
          收益 / 基准
        </div>
        <div className="text-sm">
          <span style={{ color: colorFor(verification?.returnPct) }}>
            {pctText(verification?.returnPct)}
          </span>
          <span className="mx-1 text-[var(--color-text-tertiary)]">/</span>
          <span style={{ color: colorFor(verification?.benchmarkReturnPct) }}>
            {pctText(verification?.benchmarkReturnPct)}
          </span>
        </div>
      </div>

      <div className="text-right">
        <div className="text-sm font-medium" style={{ color: statusTone(verification) }}>
          {statusLabel(verification)}
        </div>
        {verification?.excessReturnPct != null && (
          <div
            className="text-xs"
            style={{ color: colorFor(verification.excessReturnPct) }}
          >
            超额 {pctText(verification.excessReturnPct)}
          </div>
        )}
      </div>
    </div>
  );
}

function DayCard({ day }: { day: PickDayGroup }) {
  const markets = Object.values(day.markets);

  return (
    <Card className="p-4">
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
        <div className="flex flex-baseline items-baseline gap-3">
          <h3 className="text-base font-medium text-[var(--color-text-primary)]">
            {day.pickDate}
          </h3>
          <span className="text-xs text-[var(--color-text-tertiary)]">
            {day.verifiedCount}/{day.totalCount} 已验证
          </span>
        </div>
        <div className="flex flex-wrap items-baseline gap-3 text-sm">
          {day.hitRatePct != null && (
            <span style={{ color: day.hitRatePct >= 50 ? UP_COLOR : DOWN_COLOR }}>
              命中率 {day.hitRatePct.toFixed(0)}%
            </span>
          )}
          {day.winRatePct != null && (
            <span className="text-[var(--color-text-secondary)]">
              胜率 {day.winRatePct.toFixed(0)}%
            </span>
          )}
          {day.avgReturnPct != null && (
            <span style={{ color: colorFor(day.avgReturnPct) }}>
              平均收益 {pctText(day.avgReturnPct)}
            </span>
          )}
        </div>
      </div>

      <div className="space-y-4">
        {markets.map((group) => (
          <div key={group.market}>
            <div className="mb-2 flex items-center gap-2">
              <StatusDot />
              <span className="text-sm font-medium text-[var(--color-text-secondary)]">
                {marketLabel(group.market)}
              </span>
            </div>
            <div className="space-y-2">
              {group.picks.map((pick) => (
                <PickRow key={`${group.market}-${pick.code}`} pick={pick} />
              ))}
            </div>
          </div>
        ))}
      </div>
    </Card>
  );
}

export default function PickVerificationPage() {
  const [data, setData] = useState<PickVerificationDashboard | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ReturnType<typeof getParsedApiError> | null>(
    null,
  );
  const [running, setRunning] = useState<'picks' | 'verify' | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await pickVerificationApi.getDashboard(30);
      setData(result);
    } catch (err) {
      setError(getParsedApiError(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const handlePicks = useCallback(async () => {
    setRunning('picks');
    setNotice(null);
    try {
      const stats = await pickVerificationApi.runPicks({});
      setNotice(
        `选股完成：写入 ${stats.saved} 条，跳过 ${stats.skipped} 条${
          stats.errors?.length ? `，失败 ${stats.errors.length} 个市场` : ''
        }`,
      );
      await load();
    } catch (err) {
      setError(getParsedApiError(err));
    } finally {
      setRunning(null);
    }
  }, [load]);

  const handleVerify = useCallback(async () => {
    setRunning('verify');
    setNotice(null);
    try {
      const stats = await pickVerificationApi.runVerify({});
      setNotice(
        `验证完成：完成 ${stats.done} 条，数据不足 ${stats.insufficient} 条，失败 ${stats.errors} 条`,
      );
      await load();
    } catch (err) {
      setError(getParsedApiError(err));
    } finally {
      setRunning(null);
    }
  }, [load]);

  const summary = data?.summary;
  const statCards = useMemo(
    () => [
      {
        label: '累计入选',
        value: summary ? String(summary.totalPicks) : '--',
        color: undefined as string | undefined,
      },
      {
        label: '已验证',
        value: summary ? String(summary.verifiedPicks) : '--',
        color: undefined as string | undefined,
      },
      {
        label: '命中率',
        value:
          summary?.hitRatePct != null ? `${summary.hitRatePct.toFixed(1)}%` : '--',
        color:
          summary?.hitRatePct != null
            ? summary.hitRatePct >= 50
              ? UP_COLOR
              : DOWN_COLOR
            : undefined,
      },
      {
        label: '胜率（上涨）',
        value:
          summary?.winRatePct != null ? `${summary.winRatePct.toFixed(1)}%` : '--',
        color:
          summary?.winRatePct != null
            ? summary.winRatePct >= 50
              ? UP_COLOR
              : DOWN_COLOR
            : undefined,
      },
      {
        label: '平均收益',
        value:
          summary?.avgReturnPct != null
            ? pctText(summary.avgReturnPct)
            : '--',
        color: colorFor(summary?.avgReturnPct),
      },
      {
        label: '平均超额',
        value:
          summary?.avgExcessReturnPct != null
            ? pctText(summary.avgExcessReturnPct)
            : '--',
        color: colorFor(summary?.avgExcessReturnPct),
      },
    ],
    [summary],
  );

  return (
    <div className="mx-auto w-full max-w-6xl px-4 py-6 sm:px-6">
      <div className="mb-6 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-medium text-[var(--color-text-primary)]">
            选股验证看板
          </h1>
          <p className="mt-1 text-sm text-[var(--color-text-secondary)]">
            每日 A 股 / 美股各取前 5 名，次日收盘验证是否上涨且跑赢基准
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            onClick={handlePicks}
            disabled={running !== null}
            className="h-10 rounded-xl border border-[var(--color-border-secondary)] px-4 text-sm text-[var(--color-text-primary)] transition-opacity hover:opacity-80 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {running === 'picks' ? '选股中…' : '执行每日选股'}
          </button>
          <button
            type="button"
            onClick={handleVerify}
            disabled={running !== null}
            className="h-10 rounded-xl border border-[var(--color-border-secondary)] px-4 text-sm text-[var(--color-text-primary)] transition-opacity hover:opacity-80 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {running === 'verify' ? '验证中…' : '执行次日验证'}
          </button>
          <button
            type="button"
            onClick={load}
            disabled={loading}
            className="h-10 rounded-xl border border-[var(--color-border-secondary)] px-4 text-sm text-[var(--color-text-primary)] transition-opacity hover:opacity-80 disabled:cursor-not-allowed disabled:opacity-60"
          >
            刷新
          </button>
        </div>
      </div>

      {notice && (
        <div className="mb-4 rounded-xl border border-[var(--color-border-tertiary)] px-4 py-2 text-sm text-[var(--color-text-secondary)]">
          {notice}
        </div>
      )}

      <div className="mb-6 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        {statCards.map((item) => (
          <Card key={item.label} className="p-4">
            <div className="text-xs text-[var(--color-text-tertiary)]">
              {item.label}
            </div>
            <div
              className="mt-1 text-xl font-medium"
              style={{ color: item.color || 'var(--color-text-primary)' }}
            >
              {item.value}
            </div>
          </Card>
        ))}
      </div>

      {error && (
        <div className="mb-4">
          <ApiErrorAlert error={error} />
        </div>
      )}

      {loading ? (
        <div className="py-16 text-center text-sm text-[var(--color-text-tertiary)]">
          加载中…
        </div>
      ) : !data || data.days.length === 0 ? (
        <EmptyState
          title="暂无选股记录"
          description="点击「执行每日选股」或运行 python main.py --screen-picks 生成当日选股后即可查看"
        />
      ) : (
        <div className="space-y-4">
          {data.days.map((day) => (
            <DayCard key={day.pickDate} day={day} />
          ))}
        </div>
      )}
    </div>
  );
}
