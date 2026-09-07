"use client";

import {
  Bar,
  BarChart,
  CartesianGrid,
  LabelList,
  ResponsiveContainer,
  XAxis,
  YAxis,
} from "recharts";

const INK = "#1A1D21";
const RULE = "#DFDCD4";
const SOFT = "#5C6169";

export default function LeadTimeChart({
  data,
}: {
  data: { apw: number; price: number }[];
}) {
  if (!data.length) return null;
  return (
    <div className="chart" style={{ height: 260 }}>
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ top: 20, right: 8, bottom: 8, left: 0 }}>
          <CartesianGrid stroke={RULE} vertical={false} />
          <XAxis
            dataKey="apw"
            tick={{ fill: SOFT, fontSize: 12 }}
            tickLine={false}
            axisLine={{ stroke: RULE }}
            tickFormatter={(v) => `T+${v}`}
          />
          <YAxis
            tick={{ fill: SOFT, fontSize: 12 }}
            tickLine={false}
            axisLine={false}
            width={56}
            tickFormatter={(v) => `₹${(v / 1000).toFixed(0)}k`}
          />
          <Bar dataKey="price" fill={INK} isAnimationActive={false} maxBarSize={56}>
            <LabelList
              dataKey="price"
              position="top"
              fill={SOFT}
              fontSize={12}
              formatter={(v: number) => `₹${Math.round(v).toLocaleString("en-IN")}`}
            />
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
