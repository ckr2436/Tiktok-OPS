import HermesAgentPage from './HermesAgentPage.jsx'

export default function VideoAnalysisPage() {
  return (
    <HermesAgentPage
      title="短视频拆解助手"
      description="输入视频链接或文案要点，自动产出视频结构拆解、钩子分析与优化建议。"
      endpoint="video-analysis"
      permissionKey="hermes_agent.video_analysis"
      fields={[
        { name: 'video_url', label: '视频链接', required: true, placeholder: 'https://www.tiktok.com/...' },
        { name: 'product', label: '关联产品', placeholder: '可选，用于生成更精准建议' },
        { name: 'analysis_focus', label: '拆解重点', placeholder: '例如：开场钩子、镜头节奏、转化口播' },
      ]}
    />
  )
}
