"use client";

import { BarChart, LineChart, PieChart } from "echarts/charts";
import { GridComponent, LegendComponent, TooltipComponent } from "echarts/components";
import * as echarts from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { useEffect, useRef } from "react";

import type { ChartSpec, TableData } from "@/lib/api";
import { CHART_THEME, useIsDark } from "@/lib/theme";

echarts.use([BarChart, LineChart, PieChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer]);

const MAX_POINTS = 60; // 超过就只画前 60 个，避免柱子挤成一条线

export function ChartView({ spec, data }: { spec: ChartSpec; data: TableData }) {
  const ref = useRef<HTMLDivElement>(null);
  const dark = useIsDark();

  useEffect(() => {
    if (!ref.current || !spec.x || !spec.y?.length) return;
    const t = dark ? CHART_THEME.dark : CHART_THEME.light;
    const xi = data.columns.indexOf(spec.x);
    const yis = spec.y.map((c) => data.columns.indexOf(c));
    const rows = data.rows.slice(0, MAX_POINTS);
    const labels = rows.map((r) => String(r[xi] ?? "(空)"));
    const chart = echarts.init(ref.current, undefined, { renderer: "canvas" });
    const tooltip = {
      backgroundColor: t.tooltipBg,
      borderColor: t.tooltipBorder,
      textStyle: { color: t.tooltipText, fontSize: 12 },
    };

    if (spec.type === "pie") {
      chart.setOption({
        color: t.series,
        tooltip: { ...tooltip, trigger: "item" },
        legend: { bottom: 0, textStyle: { color: t.text }, icon: "circle", itemWidth: 8 },
        series: [
          {
            type: "pie",
            radius: ["48%", "72%"],
            center: ["50%", "45%"],
            itemStyle: { borderColor: dark ? "#16181c" : "#fbfbfc", borderWidth: 2 },
            label: { color: t.text, formatter: "{b}  {d}%" },
            data: rows.map((r) => ({ name: String(r[xi] ?? "(空)"), value: r[yis[0]] })),
          },
        ],
      });
    } else {
      chart.setOption({
        color: t.series,
        // 标签倾斜时第一个标签会伸出左边界，多留出空间
        grid: { left: labels.length > 5 ? 40 : 8, right: 16, top: 24, bottom: 8, containLabel: true },
        tooltip: { ...tooltip, trigger: "axis" },
        legend: spec.y.length > 1 ? { top: 0, textStyle: { color: t.text } } : undefined,
        xAxis: {
          type: "category",
          data: labels,
          axisLine: { lineStyle: { color: t.axis } },
          axisTick: { show: false },
          // 类别多时倾斜并截断，而不是让 ECharts 静默隐藏大部分标签
          axisLabel: {
            color: t.text,
            interval: 0,
            rotate: labels.length > 5 ? 30 : 0,
            width: 96,
            overflow: "truncate",
          },
        },
        yAxis: {
          type: "value",
          splitLine: { lineStyle: { color: t.split } },
          axisLabel: { color: t.text },
        },
        series: spec.y.map((name, k) => ({
          name,
          type: spec.type === "line" ? "line" : "bar",
          data: rows.map((r) => r[yis[k]]),
          smooth: spec.type === "line" ? 0.2 : undefined,
          showSymbol: rows.length <= 24,
          barMaxWidth: 28,
          itemStyle: spec.type === "bar" ? { borderRadius: [4, 4, 0, 0] } : undefined,
        })),
      });
    }
    const resize = new ResizeObserver(() => chart.resize());
    resize.observe(ref.current);
    return () => {
      resize.disconnect();
      chart.dispose();
    };
  }, [spec, data, dark]);

  return <div ref={ref} className="h-72 w-full" />;
}
