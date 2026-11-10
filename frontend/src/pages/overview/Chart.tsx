import { useEffect, useRef } from 'react'
import * as echarts from 'echarts/core'
import { BarChart, GaugeChart, GraphChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([BarChart, GaugeChart, GraphChart, GridComponent, TooltipComponent, CanvasRenderer])

/** An ECharts canvas that follows the size of its box and redraws when the option changes. */
export default function Chart({ option, className, label }: { option: echarts.EChartsCoreOption; className?: string; label: string }) {
  const box = useRef<HTMLDivElement>(null)
  const chart = useRef<echarts.ECharts | null>(null)
  useEffect(() => {
    const instance = echarts.init(box.current!, undefined, { renderer: 'canvas' })
    chart.current = instance
    const observer = new ResizeObserver(() => instance.resize())
    observer.observe(box.current!)
    return () => { observer.disconnect(); instance.dispose(); chart.current = null }
  }, [])
  useEffect(() => { chart.current?.setOption(option, true) }, [option])
  return <div ref={box} role="img" aria-label={label} className={className} />
}
