import { memo } from 'react'
import { Handle, Position, type NodeProps } from '@xyflow/react'
import { Cpu, Loader2, CheckCircle, XCircle, MinusCircle, AlertTriangle } from 'lucide-react'

const STATUS_ICON: Record<string, React.ReactNode> = {
  running: <Loader2 size={11} className="text-amber-500 animate-spin" />,
  success: <CheckCircle size={11} className="text-green-500" />,
  degraded: <AlertTriangle size={11} className="text-amber-600" />,
  failed: <XCircle size={11} className="text-red-500" />,
  skipped: <MinusCircle size={11} className="text-gray-400" />,
}

function TransformNode({ data, selected }: NodeProps) {
  const label = typeof data.label === 'string' ? data.label : '转换器'
  const status = typeof data.status === 'string' ? data.status : 'idle'
  const statusReason = typeof data.statusReason === 'string' ? data.statusReason : ''
  const config = data.config as { steps?: unknown[] } | undefined
  const steps = config?.steps ?? []

  return (
    <div title={statusReason || (status === 'skipped' ? '执行器当前未单独执行此节点' : undefined)} className={`px-3 py-2 rounded-xl border-2 shadow-sm bg-white text-xs min-w-[140px] relative ${
      selected ? 'border-amber-500 ring-2 ring-amber-200' :
      status === 'running' ? 'border-amber-400 ring-2 ring-amber-100' :
      status === 'success' ? 'border-green-400' :
      status === 'degraded' ? 'border-amber-400 ring-2 ring-amber-100' :
      status === 'failed' ? 'border-red-400' :
      status === 'skipped' ? 'border-gray-300 border-dashed' :
      'border-amber-300'
    }`}>
      {status !== 'idle' && (
        <div className="absolute -top-2 -right-2 bg-white rounded-full p-0.5 shadow-sm">
          {STATUS_ICON[status]}
        </div>
      )}
      <Handle type="target" position={Position.Left} className="!bg-amber-500 !w-2 !h-2" />
      <Handle type="source" position={Position.Right} className="!bg-amber-500 !w-2 !h-2" />
      <div className="flex items-center gap-1.5">
        <Cpu size={13} className="text-amber-500" />
        <span className="font-medium text-amber-700">{label}</span>
      </div>
      <p className="text-gray-400 mt-0.5">{steps.length > 0 ? `${steps.length} 个步骤` : '转换器'}</p>
    </div>
  )
}

export default memo(TransformNode)
