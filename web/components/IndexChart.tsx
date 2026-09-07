"use client";

import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { DailyPoint } from "@/lib/api";

const INK = "#1A1D21";
const RULE = "#DFDCD4";
const SOFT = "#5C6169";

export default function IndexChart({ data }: { data: DailyPoint[] }) {
  if (!data.length) return null;

  const values = data.map((d) => d.apix);
  const lo = Math.floor(Math.min(...values, 100) - 2);
  const hi = Math.ceil(Math.max(...values, 100) + 2);

  return (
    <div className="chart">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 8, right: 8, bottom: 8, left: 0 }}>
          <CartesianGrid stroke={RULE} vertical={false} />
          <XAxis
            dataKey="quote_date"
            tick={{ fill: SOFT, fontSize: 12 }}
            tickLine={false}
            axisLine={{ stroke: RULE }}
            minTickGap={48}
            tickFormatter={(v: string) =>
              new Date(v).toLocaleDateString("en-IN", {
                day: "numeric",
                month: "short",
              })
            }
          />
          <YAxis
            domain={[lo, hi]}
            tick={{ fill: SOFT, fontSize: 12 }}
            tickLine={false}
            axisLine={false}
            width={44}
          />
          <Tooltip
            contentStyle={{
              background: "#FBFAF7",
              border: `1px solid ${INK}`,
              borderRadius: 2,
              fontSize: 13,
            }}
            labelFormatter={(v) =>
              new Date(v as string).toLocaleDateString("en-IN", {
                day: "numeric",
                month: "long",
                year: "numeric",
              })
            }
            formatter={(v: number) => [v.toFixed(2), "APIx"]}
          />
          {/* Base = 100. Without this line the reader has no anchor for
              whether a value is high or low. */}
          <Line
            type="monotone"
            dataKey={() => 100}
            stroke={RULE}
            strokeWidth={1}
            dot={false}
            activeDot={false}
            isAnimationActive={false}
          />
          <Line
            type="monotone"
            dataKey="apix"
            stroke={INK}
            strokeWidth={1.75}
            dot={false}
            isAnimationActive={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
