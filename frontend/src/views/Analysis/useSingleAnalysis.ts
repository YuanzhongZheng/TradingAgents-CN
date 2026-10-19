import ResearchDefaults from '@/components/Research/ResearchDefaults.vue'
import { searchStocks } from '@/api/multiMarket'
import { ref, reactive, onMounted, onUnmounted, computed, watch } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  Document,
  TrendCharts,
  InfoFilled,
  Check,
  Loading,
  Refresh,
  Download,
  WarningFilled,
  Cpu,
  Plus,
  DocumentChecked,
  View,
} from '@element-plus/icons-vue'
import { analysisApi, type SingleAnalysisRequest } from '@/api/analysis'
import { workflowApi, isSelectableAnalysisWorkflow, type WorkflowSummary } from '@/api/workflow'
import { stocksApi, type FundamentalsResponse } from '@/api/stocks'
import { stockSyncApi, type StockSyncTaskStatus, type SingleStockSyncResponse } from '@/api/stockSync'
import { useAppStore } from '@/stores/app'
import { useAuthStore } from '@/stores/auth'
import { ApiClient } from '@/api/request'
import { configApi } from '@/api/config'
import DeepModelSelector from '@/components/DeepModelSelector.vue'
import TaskGrowthPanel from '@/components/growth/TaskGrowthPanel.vue'
import { ANALYSTS, convertAnalystNamesToIds } from '@/constants/analysts'
import { marked } from 'marked'
import { recommendModels } from '@/api/modelCapabilities'
import { validateStockCode, getStockCodeFormatHelp } from '@/utils/stockValidator'
import { normalizeMarketForAnalysis, getMarketByStockCode } from '@/utils/market'
import { fetchTaskResultWithRetry, hasTaskResultContent, mergeTaskResult } from '@/utils/taskResult'
import type { StockInfo } from '@/types/analysis'

// 配置marked选项
marked.setOptions({
  breaks: true,        // 支持换行符转换为<br>
  gfm: true           // 启用GitHub风格的Markdown
})

// 市场类型定义
type MarketType = 'A股' | 'ETF'

// 分析引擎类型
type AnalysisEngineType = 'legacy' | 'v2'

// 表单类型定义
interface AnalysisForm {
  stockCode: string
  symbol: string
  market: MarketType
  analysisDate: Date
  researchDepth: number
  selectedAnalysts: string[]
  includeSentiment: boolean
  includeRisk: boolean
  language: 'zh-CN' | 'en-US'
  engine: AnalysisEngineType  // 分析引擎: 固定使用 v2.0 引擎
  enableTrace: boolean  // 执行轨迹开关：默认关闭，排查问题时可开启
}

// 使用store

export function useSingleAnalysis() {
const router = useRouter()
const route = useRoute()

const submitting = ref(false)
const validatingData = ref(false)
const showValidationDialog = ref(false)
const validationResult = ref<any>(null)
const factorPreviewLoading = ref(false)
const factorPreview = ref<FundamentalsResponse | null>(null)
const syncStatus = ref<'idle' | 'running' | 'success' | 'error'>('idle')
const syncPollingTimer = ref<any>(null)

// 分析进度和结果相关状态
const currentTaskId = ref('')
const analysisStatus = ref('idle') // 'idle', 'running', 'completed', 'failed'
const showResults = ref(false)
const analysisResults = ref<any>(null)
const analysisReportManifest = ref<Array<{ field: string; display_name: string; icon?: string; description?: string }>>([])
const activeReportTab = ref('') // 当前激活的报告标签页
const growthTaskId = computed(() => analysisResults.value?.task_id || currentTaskId.value || null)
const progressInfo = ref({
  progress: 0,
  currentStep: '',
  currentStepDescription: '',  // 当前步骤描述
  message: '',
  elapsedTime: 0,      // 已用时间（秒）
  remainingTime: 0,    // 预计剩余时间（秒）
  totalTime: 0         // 预计总时长（秒）
})
const pollingTimer = ref<any>(null)

const applyAnalysisResult = (result: any) => {
  analysisResults.value = result
  activeReportTab.value = ''
}

const loadCompletedAnalysisResult = async (taskId: string, status?: any) => {
  const statusResult = status?.result_data && typeof status.result_data === 'object' ? status.result_data : null
  const finalResult = await fetchTaskResultWithRetry(taskId, {
    seedData: statusResult,
    isComplete: result => {
      const merged = mergeTaskResult(statusResult, result)
      console.log('📦 获取分析结果尝试:', {
        hasContent: hasTaskResultContent(merged),
        hasDecision: !!merged?.decision,
        hasSummary: !!merged?.summary,
        reportCount: Object.keys(merged?.reports || {}).length
      })
      return hasTaskResultContent(merged)
    }
  })

  return mergeTaskResult(statusResult, finalResult)
}

// 分析步骤定义（动态生成）
const analysisSteps = ref<any[]>([])

// 从后端步骤数据生成前端步骤
const generateStepsFromBackend = (backendSteps: any[]) => {
  if (!backendSteps || !Array.isArray(backendSteps)) {
    return []
  }

  return backendSteps.map((step: any, index: number) => ({
    key: `step_${index}`,
    title: step.name || `步骤 ${index + 1}`,
    description: step.description || '处理中...',
    status: 'pending'
  }))
}

const showAdvanced = ref(false)
const modelsReady = ref(false)
const modelLoadError = ref('')
const formatResearchDate = (value: Date | string) => {
  const date = new Date(value)
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`
}
let stockSearchVersion = 0
const searchStockSuggestions = async (query: string) => {
  const version = ++stockSearchVersion
  if (!query.trim()) return []
  try {
    const response = await searchStocks('CN', query.trim(), 10)
    if (version !== stockSearchVersion) return []
    return (response.data?.stocks || []).map(stock => ({ value: stock.code, label: `${stock.name} · ${stock.code}` }))
  } catch {
    return []
  }
}
const selectStockSuggestion = (stock: Record<string, any>) => {
  analysisForm.stockCode = stock.value
  inferMarketFromCode()
  validateStockCodeInput()
}
const inferMarketFromCode = () => {
  const code = analysisForm.stockCode.trim()
  if (/^\d{6}$/.test(code)) {
    const market = validateStockCode(code, 'ETF').valid ? 'ETF' : 'A股'
    if (analysisForm.market !== market) {
      analysisForm.market = market
      analysisForm.selectedAnalysts = normalizeSelectedAnalystsForMarket(market, analysisForm.selectedAnalysts)
    }
  }
}

// 模型设置
const modelSettings = ref({
  quickAnalysisModel: 'qwen-turbo',
  deepAnalysisModel: 'qwen-max'
})

// 可用的模型列表（从配置中获取）
const availableModels = ref<any[]>([])
const workflows = ref<WorkflowSummary[]>([])
const workflowsSorted = computed(() => {
  // 根据市场类型做级联过滤
  const expectedType = analysisForm.market === 'ETF' ? 'etf_analysis' : 'stock_analysis'
  const arr = workflows.value.filter(
    (w: WorkflowSummary) => isSelectableAnalysisWorkflow(w, expectedType)
  )
  
  // 按版本号倒序排序
  arr.sort((a, b) => {
    const vA = a.version || ''
    const vB = b.version || ''
    const verA = parseVersion(vA)
    const verB = parseVersion(vB)
    
    for (let i = 0; i < Math.max(verA.length, verB.length); i++) {
      const numA = verA[i] || 0
      const numB = verB[i] || 0
      if (numA !== numB) {
        return numB - numA
      }
    }
    return 0
  })
  
  return arr
})

// 解析版本号为数字数组
function parseVersion(v: string | number): number[] {
  const str = String(v || '')
  return str.split('.').map(p => parseInt(p.trim(), 10) || 0)
}
const selectedWorkflowId = ref<string>('v2_stock_analysis')

// 🆕 模型推荐提示
const modelRecommendation = ref<{
  title: string
  message: string
  type: 'success' | 'warning' | 'info' | 'error'
  quickModel?: string
  deepModel?: string
} | null>(null)

// 分析表单
const analysisForm = reactive<AnalysisForm>({
  stockCode: '',  // 保留用于表单绑定
  symbol: '',     // 标准化后的代码
  market: 'A股',
  analysisDate: new Date(),
  researchDepth: 3, // 默认选中3级标准分析（推荐），将在 onMounted 中从用户偏好加载
  selectedAnalysts: ['市场分析师', '基本面分析师'], // 将在 onMounted 中从用户偏好加载
  includeSentiment: true,
  includeRisk: true,
  language: 'zh-CN',
  engine: 'v2',  // 固定使用 v2.0 引擎
  enableTrace: false  // 执行轨迹：默认关闭，排查问题时可开启
})

// 监听市场类型变化，自动切换到对应类型的默认流程
watch(
  () => analysisForm.market,
  () => {
    // 使用已过滤（排除 v1.0/快速/空白流程）并按版本排序的列表
    const targetWorkflows = workflowsSorted.value
    
    if (targetWorkflows.length > 0) {
      // 优先找 is_default 的
      const defaultWf = targetWorkflows.find(w => w.is_default)
      if (defaultWf) {
        selectedWorkflowId.value = defaultWf.id
      } else {
        // 没找到的话选第一个（最高版本）
        selectedWorkflowId.value = targetWorkflows[0].id
      }
    }
  }
)

// 股票代码验证相关
const stockCodeError = ref<string>('')
const stockCodeHelp = ref<string>('')

// 深度选项（5个级别，基于实际测试数据更新）
const depthOptions = [
  { icon: '⚡', name: '1级 - 快速分析', description: '基础数据概览，快速决策', time: '2-5分钟' },
  { icon: '📈', name: '2级 - 基础分析', description: '常规投资决策', time: '3-6分钟' },
  { icon: '🎯', name: '3级 - 标准分析', description: '技术+基本面，推荐', time: '4-8分钟' },
  { icon: '🔍', name: '4级 - 深度分析', description: '多轮辩论，深度研究', time: '6-11分钟' },
  { icon: '🏆', name: '5级 - 全面分析', description: '最全面的分析报告', time: '8-16分钟' }
]

// 禁用日期
const disabledDate = (time: Date) => {
  return time.getTime() > Date.now()
}

const factorPreviewRow = computed<StockInfo>(() => ({
  symbol: analysisForm.stockCode || '',
  name: analysisForm.stockCode || '',
  market: analysisForm.market,
  report_period: factorPreview.value?.report_period,
  factor_diagnostics: factorPreview.value?.factor_diagnostics || {},
  factor_warnings: factorPreview.value?.factor_warnings || {},
}))

const clearFactorPreview = () => {
  factorPreview.value = null
}

const fetchFactorPreview = async (normalizedCode?: string) => {
  if (analysisForm.market !== 'A股') {
    clearFactorPreview()
    return
  }

  const targetCode = String(normalizedCode || analysisForm.stockCode || '').trim()
  if (!targetCode) {
    clearFactorPreview()
    return
  }

  factorPreviewLoading.value = true
  try {
    const response = await stocksApi.getFundamentals(targetCode)
    factorPreview.value = ((response as any)?.data || null) as FundamentalsResponse | null
  } catch (error) {
    console.warn('获取因子预览失败', error)
    clearFactorPreview()
  } finally {
    factorPreviewLoading.value = false
  }
}

// 股票代码输入时的处理
const onStockCodeInput = (value: string) => {
  analysisForm.stockCode = value
  if (!showAdvanced.value) inferMarketFromCode()
  // 清除错误信息
  stockCodeError.value = ''
  clearFactorPreview()
  // 显示格式提示
  stockCodeHelp.value = /[^0-9]/.test(analysisForm.stockCode) ? '请从搜索结果中选择股票，或直接输入六位代码。' : getStockCodeFormatHelp(analysisForm.market)
}

const ETF_ANALYST_NAMES = ['ETF分析师', '市场分析师', '新闻分析师']

const normalizeSelectedAnalystsForMarket = (market: MarketType, analysts: string[]) => {
  if (market === 'ETF') {
    return [...ETF_ANALYST_NAMES]
  }

  const disallowedAnalysts = ['ETF分析师']
  if (market !== 'A股') {
    disallowedAnalysts.push('大盘分析师', '板块分析师')
  }

  return analysts.filter(name => !disallowedAnalysts.includes(name))
}

// 🔥 根据市场类型过滤可用的分析师（大盘和板块分析师仅支持A股，ETF 仅显示 etf/market/news）
const availableAnalysts = computed(() => {
  const isAStock = analysisForm.market === 'A股'
  const isETF = analysisForm.market === 'ETF'
  const etfAnalystIds = ['etf', 'market', 'news']

  return ANALYSTS.filter(analyst => {
    if (isETF) return etfAnalystIds.includes(analyst.id)
    if (analyst.id === 'etf') return false
    return true
  }).map(analyst => {
    const isMacroAnalyst = analyst.id === 'index_analyst' || analyst.id === 'sector_analyst'
    return {
      ...analyst,
      disabled: !isAStock && isMacroAnalyst
    }
  })
})

// 市场类型变更时的处理
const onMarketChange = () => {
  clearFactorPreview()
  // 重新验证股票代码
  if (analysisForm.stockCode.trim()) {
    validateStockCodeInput()
  } else {
    // 显示新市场的格式提示
    stockCodeHelp.value = getStockCodeFormatHelp(analysisForm.market)
  }

  // 🔥 ETF 市场：固定使用 ETF / 市场 / 新闻 分析师
  if (analysisForm.market === 'ETF') {
    analysisForm.selectedAnalysts = [...ETF_ANALYST_NAMES]
    return
  }

  analysisForm.selectedAnalysts = normalizeSelectedAnalystsForMarket(
    analysisForm.market,
    analysisForm.selectedAnalysts
  )
}

// 验证股票代码输入
const validateStockCodeInput = () => {
  const code = analysisForm.stockCode.trim()

  if (!code) {
    stockCodeError.value = ''
    stockCodeHelp.value = ''
    return
  }

  if (!showAdvanced.value) inferMarketFromCode()
  // 验证股票代码格式
  let validation = validateStockCode(code, analysisForm.market)

  if (!validation.valid) {
    // 🔥 智能提示：A股验证失败但代码符合ETF格式时，提示用户切换市场
    if (analysisForm.market === 'A股') {
      const etfCheck = validateStockCode(code, 'ETF')
      if (etfCheck.valid) {
        stockCodeError.value = '该代码为ETF基金，请将市场类型改为「ETF 基金」'
        stockCodeHelp.value = ''
        return
      }
    }
    stockCodeError.value = validation.message || '股票代码格式不正确'
    stockCodeHelp.value = ''
  } else {
    stockCodeError.value = ''
    stockCodeHelp.value = `✓ ${validation.market}代码格式正确`

    // 自动更新市场类型（如果识别出的市场与当前选择不同）
    // V3.0 聚焦 A 股，港美股已暂停
    if (validation.market && validation.market !== analysisForm.market) {
      const validMarkets: MarketType[] = ['A股', 'ETF']
      if ((validMarkets as string[]).includes(validation.market)) {
        analysisForm.market = validation.market as MarketType
        onMarketChange()  // 同步更新分析师选择（如 ETF 固定分析师）
        ElMessage.success(`已自动识别为${validation.market}`)
      }
    }

    // 标准化代码
    if (validation.normalizedCode) {
      analysisForm.stockCode = validation.normalizedCode
    }
  }

  // 获取股票信息
  fetchStockInfo()
}

// 获取股票信息（实时行情，用于显示股票名称和当前价格）
const fetchStockInfo = async () => {
  const code = analysisForm.stockCode.trim()
  if (!code) return

  try {
    const response = await stocksApi.getQuote(code)
    const data = (response as any)?.data
    if (data) {
      // 更新 factorPreviewRow 中的名称（如果行情返回了名称）
      if (data.name && factorPreviewRow.value) {
        factorPreviewRow.value.name = data.name
      }
    }
  } catch (error) {
    // 行情获取失败不影响主流程，静默处理
    console.warn('获取股票行情失败', error)
  }
}

// 切换分析师
const toggleAnalyst = (analystName: string) => {
  const index = analysisForm.selectedAnalysts.indexOf(analystName)
  if (index > -1) {
    analysisForm.selectedAnalysts.splice(index, 1)
  } else {
    analysisForm.selectedAnalysts.push(analystName)
  }
}

// 数据校验
const handleValidateData = async () => {
  const stockCode = analysisForm.stockCode.trim()
  if (!stockCode) {
    ElMessage.warning('请先输入股票代码')
    return
  }

  // 验证股票代码格式
  let validation = validateStockCode(stockCode, analysisForm.market)
  if (!validation.valid) {
    if (analysisForm.market === 'A股') {
      const etfCheck = validateStockCode(stockCode, 'ETF')
      if (etfCheck.valid) {
        ElMessage.warning('该代码为ETF基金，请将市场类型改为「ETF 基金」后再进行数据校验')
        return
      }
    }
    ElMessage.error(validation.message || '股票代码格式不正确')
    return
  }

  // 使用标准化后的代码
  const normalizedCode = validation.normalizedCode || stockCode.toUpperCase()

  // 确定市场类型（用于API调用）
  const marketTypeMap: Record<string, string> = {
    'A股': 'cn',
    'ETF': 'cn'  // ETF 为 A 股场内基金
  }
  const marketType = marketTypeMap[analysisForm.market] || 'cn'

  // 格式化分析日期
  const analysisDate = analysisForm.analysisDate instanceof Date
    ? analysisForm.analysisDate.toISOString().split('T')[0]
    : analysisForm.analysisDate

  validatingData.value = true

  try {
    const response = await stocksApi.validateData(
      normalizedCode,
      analysisDate,
      marketType,
      {
        check_basic_info: true,
        check_historical_data: true,
        check_financial_data: true,
        check_realtime_quotes: true
      }
    )

    if (response.data) {
      const result = response.data
      await fetchFactorPreview(normalizedCode)
      
      // 显示校验结果对话框
      showValidationResultDialog(result)
    } else {
      ElMessage.error('数据校验失败：未返回结果')
    }
  } catch (error: any) {
    console.error('数据校验失败:', error)
    ElMessage.error(`数据校验失败：${error.response?.data?.detail || error.message || '未知错误'}`)
  } finally {
    validatingData.value = false
  }
}

const sleep = (ms: number) => new Promise(resolve => window.setTimeout(resolve, ms))

const isTerminalStockSyncStatus = (status?: string): boolean => {
  return status === 'success' || status === 'success_with_errors' || status === 'failed'
}

const buildSingleSyncTaskMessage = (task: StockSyncTaskStatus, symbol: string): string => {
  const result = task.result as SingleStockSyncResponse | null | undefined
  if (!result) {
    return task.error || task.message || `股票 ${symbol} 同步失败`
  }

  const parts = [`股票 ${symbol} 数据同步完成`]

  if (result.realtime_sync) {
    if (result.realtime_sync.success) {
      const dataSourceUsed = (result.realtime_sync as any).data_source_used
      if (dataSourceUsed) {
        parts.push(`实时行情同步成功（数据源 ${String(dataSourceUsed).toUpperCase()}）`)
      } else {
        parts.push('实时行情同步成功')
      }
    } else {
      parts.push(`实时行情同步失败: ${result.realtime_sync.error || '未知错误'}`)
    }
  }

  if (result.historical_sync) {
    parts.push(result.historical_sync.success ? `历史数据 ${result.historical_sync.records || 0} 条` : `历史数据同步失败: ${result.historical_sync.error || '未知错误'}`)
  }
  if (result.financial_sync) {
    parts.push(result.financial_sync.success ? '财务数据同步成功' : `财务数据同步失败: ${result.financial_sync.error || '未知错误'}`)
  }
  if (result.basic_sync) {
    parts.push(result.basic_sync.success ? '基础数据同步成功' : `基础数据同步失败: ${result.basic_sync.error || '未知错误'}`)
  }

  return parts.join('；')
}

const STOCK_SYNC_POLL_MAX_ATTEMPTS = 60
const STOCK_SYNC_POLL_INTERVAL_MS = 2000

const waitForStockSyncTask = async (taskId: string): Promise<StockSyncTaskStatus> => {
  for (let attempt = 0; attempt < STOCK_SYNC_POLL_MAX_ATTEMPTS; attempt += 1) {
    const res = await stockSyncApi.getTaskStatus(taskId)
    if (res.success && isTerminalStockSyncStatus(res.data.status)) {
      return res.data
    }

    await sleep(STOCK_SYNC_POLL_INTERVAL_MS)
  }

  throw new Error('同步任务执行超时，请稍后刷新页面查看结果')
}

const monitorSingleSyncTask = async (taskId: string, symbol: string) => {
  try {
    const task = await waitForStockSyncTask(taskId)
    const message = buildSingleSyncTaskMessage(task, symbol)
    if (task.status === 'success') {
      ElMessage.success(message)
    } else if (task.status === 'success_with_errors') {
      ElMessage.warning(message)
    } else {
      ElMessage.error(message)
    }

    // 同步完成后，重新调用数据检验
    await handleValidateData()
  } catch (error: any) {
    if (error?.message !== '页面已离开，停止等待同步结果') {
      ElMessage.error(error?.message || '同步结果查询失败，请稍后刷新页面查看')
    }
  } finally {
    clearSyncPolling()
  }
}

// 同步缺失数据
const handleSyncMissingData = async () => {
  const stockCode = analysisForm.stockCode.trim()
  const validation = validateStockCode(stockCode, analysisForm.market)

  if (!validation.valid) {
    ElMessage.error('股票代码不正确')
    return
  }

  const normalizedCode = validation.normalizedCode || stockCode.toUpperCase()
  const missingData = validationResult.value?.missing_data || [] as string[]

  // 根据缺失数据确定同步什么
  const syncTypes: string[] = []
  if (missingData.includes('实时行情')) syncTypes.push('realtime')
  if (missingData.includes('历史数据')) syncTypes.push('historical')
  if (missingData.includes('财务数据')) syncTypes.push('financial')
  if (missingData.includes('基础信息')) syncTypes.push('basic')

  // 如果没有明确指定缺失，默认都同步
  if (syncTypes.length === 0) {
    syncTypes.push('realtime', 'historical', 'financial', 'basic')
  }

  try {
    syncStatus.value = 'running'
    const res = await stockSyncApi.syncSingle({
      symbol: normalizedCode,
      sync_realtime: syncTypes.includes('realtime'),
      sync_historical: syncTypes.includes('historical'),
      sync_financial: syncTypes.includes('financial'),
      sync_basic: syncTypes.includes('basic'),
      data_source: 'akshare',
      days: 365
    })

    if (res.success) {
      ElMessage.success(`股票 ${normalizedCode} 同步任务已提交，后台执行中`)
      void monitorSingleSyncTask(res.data.task_id, normalizedCode)
    } else {
      syncStatus.value = 'error'
      ElMessage.error(res.message || '同步失败')
    }
  } catch (error: any) {
    console.error('同步失败:', error)
    syncStatus.value = 'error'
    ElMessage.error(error.message || '同步失败，请稍后重试')
  }
}

// 清理同步轮询定时器
const clearSyncPolling = () => {
  if (syncPollingTimer.value) {
    clearInterval(syncPollingTimer.value)
    syncPollingTimer.value = null
  }
}

// 显示校验结果对话框
const showValidationResultDialog = (result: any) => {
  validationResult.value = result
  showValidationDialog.value = true
  syncStatus.value = 'idle' // 重置同步状态
}

// 格式化校验结果详情为表格数据
const formatValidationDetails = (details: any) => {
  const tables: Array<{ title: string; data: Array<{ label: string; value: any }> }> = []
  
  // 基础信息
  if (details.basic_info) {
    const basicInfo = details.basic_info
    tables.push({
      title: '基础信息',
      data: [
        { label: '状态', value: basicInfo.exists ? '✅ 存在' : '❌ 不存在' },
        { label: analysisForm.market === 'ETF' ? 'ETF名称' : '股票名称', value: basicInfo.name || '-' },
        { label: '上市日期', value: basicInfo.list_date || '-' }
      ]
    })
  }
  
  // 历史数据
  if (details.historical_data) {
    const histData = details.historical_data
    tables.push({
      title: '历史数据',
      data: [
        { label: '状态', value: histData.exists ? '✅ 存在' : '❌ 不存在' },
        { label: '数据条数', value: histData.count || 0 },
        { label: '最早日期', value: histData.earliest_date || '-' },
        { label: '最新日期', value: histData.latest_date || '-' },
        { label: '分析日期数据', value: histData.analysis_date_exists ? '✅ 存在' : '⚠️ 不存在' },
        { label: '日期范围', value: histData.date_range || '-' },
        { label: '警告', value: histData.warning ? '⚠️ 是' : '✅ 否' }
      ]
    })
  }
  
  // 财务数据
  if (details.financial_data !== undefined) {
    const finData = details.financial_data
    tables.push({
      title: '财务数据',
      data: [
        { label: '状态', value: finData.exists ? '✅ 存在' : '❌ 不存在' },
        { label: '最新报告期', value: finData.latest_report_period || '-' },
        { label: '报告类型', value: finData.report_type || '-' }
      ]
    })
  }
  
  // 实时行情
  if (details.realtime_quotes !== undefined) {
    const quotesData = details.realtime_quotes
    tables.push({
      title: '实时行情',
      data: [
        { label: '状态', value: quotesData.exists ? '✅ 存在' : '❌ 不存在' },
        { label: '当前价格', value: quotesData.current_price ? `¥${quotesData.current_price}` : '-' },
        { label: '更新时间', value: quotesData.update_time || '-' }
      ]
    })
  }
  
  return tables
}



// 提交分析
const submitAnalysis = async () => {
  if (!modelsReady.value) {
    ElMessage.warning('请先加载有效的研究模型配置')
    return
  }
  if (!showAdvanced.value) inferMarketFromCode()
  const stockCode = analysisForm.stockCode.trim()
  if (!stockCode) {
    ElMessage.warning('请输入股票代码')
    return
  }

  // 验证股票代码格式
  let validation = validateStockCode(stockCode, analysisForm.market)
  if (!validation.valid) {
    if (analysisForm.market === 'A股') {
      const etfCheck = validateStockCode(stockCode, 'ETF')
      if (etfCheck.valid) {
        const msg = '该代码为ETF基金，请将市场类型改为「ETF 基金」后再开始分析'
        ElMessage.warning(msg)
        stockCodeError.value = msg
        return
      }
    }
    ElMessage.error(validation.message || '股票代码格式不正确')
    stockCodeError.value = validation.message || '股票代码格式不正确'
    return
  }

  // 使用标准化后的代码
  analysisForm.symbol = validation.normalizedCode || stockCode.toUpperCase()
  await fetchFactorPreview(analysisForm.symbol)

  if (analysisForm.selectedAnalysts.length === 0) {
    ElMessage.warning('请至少选择一个分析师')
    return
  }

  // 🔥 分析前预检：检查数据源配置、基础数据同步状态
  try {
    const precheckResp = await analysisApi.precheck(analysisForm.symbol)
    const precheck = precheckResp.data || precheckResp
    if (!precheck.can_submit) {
      // 阻止提交，引导用户先完成配置/同步
      const redirect = precheck.redirect || '/settings/config'
      const reason = precheck.reason || 'unknown'
      const message = precheck.message || '请先完成系统配置'

      // 根据原因设置跳转参数
      let redirectUrl = redirect
      if (precheck.redirect_tab) {
        redirectUrl = `${redirect}?tab=${precheck.redirect_tab}`
      }

      await ElMessageBox.confirm(
        message,
        '无法开始分析',
        {
          confirmButtonText: reason === 'no_datasource' ? '去配置数据源' : '去同步数据',
          cancelButtonText: '取消',
          type: 'warning',
          showClose: true,
        }
      )
      router.push(redirectUrl)
      return
    }
  } catch (precheckErr: any) {
    // 用户点击取消或预检异常时，不继续提交
    if (precheckErr === 'cancel' || precheckErr?.toString?.().includes('cancel')) {
      return
    }
    // 预检接口本身异常，降级放行（后端 Worker 还有兜底校验）
    console.warn('⚠️ 分析预检异常，降级放行:', precheckErr)
  }

  submitting.value = true

  try {
    // 确保 analysisDate 是 Date 对象
    const analysisDate = analysisForm.analysisDate instanceof Date
      ? analysisForm.analysisDate
      : new Date(analysisForm.analysisDate)

    const request: SingleAnalysisRequest = {
      symbol: analysisForm.symbol,
      stock_code: analysisForm.symbol,  // 兼容字段
      parameters: {
        market_type: analysisForm.market,
        analysis_date: formatResearchDate(analysisDate),
        research_depth: getDepthDescription(analysisForm.researchDepth),
        selected_analysts: convertAnalystNamesToIds(analysisForm.selectedAnalysts),
        include_sentiment: analysisForm.includeSentiment,
        include_risk: analysisForm.includeRisk,
        language: analysisForm.language,
        quick_analysis_model: modelSettings.value.quickAnalysisModel,
        deep_analysis_model: modelSettings.value.deepAnalysisModel,
        engine: 'v2',  // 🔥 固定使用 v2.0 引擎（旧版引擎已隐藏）
        workflow_id: selectedWorkflowId.value,
        enable_trace: analysisForm.enableTrace  // 执行轨迹开关
      }
    }

    const response = await analysisApi.startSingleAnalysis(request)

    console.log('🔍 分析响应数据:', response)
    console.log('🔍 响应数据结构:', response.data)
    console.log('🔍 任务ID:', response.data?.task_id)

    ElMessage.success('分析任务已提交，正在处理中...')

    // 响应拦截器已返回 response.data，所以直接访问 response.data.task_id
    currentTaskId.value = response.data.task_id

    if (!currentTaskId.value) {
      console.error('❌ 任务ID为空:', response)
      ElMessage.error('任务ID获取失败，请重试')
      return
    }

    console.log('✅ 任务ID设置成功:', currentTaskId.value)

    // 保存任务状态到缓存
    saveTaskToCache(currentTaskId.value, {
      parameters: { ...analysisForm },
      submitTime: new Date().toISOString()
    })

    analysisStatus.value = 'running'
    showResults.value = false
    analysisResults.value = null
    activeReportTab.value = ''
    progressInfo.value = {
      progress: 0,
      currentStep: '正在初始化分析...',
      currentStepDescription: '分析任务已提交，正在启动分析流程',
      message: '分析任务已提交，正在启动分析流程',
      elapsedTime: 0,
      remainingTime: 0,
      totalTime: 0
    }

    // 初始化空的步骤列表，等待后端数据
    analysisSteps.value = []

    // 开始轮询任务状态
    startPollingTaskStatus()

    // 立即查询一次状态（不等待第一次轮询）
    setTimeout(async () => {
      try {
        const response = await analysisApi.getTaskStatus(currentTaskId.value)
        const status = response.data // 响应拦截器已返回 response.data
        console.log('🔄 立即查询状态:', status)
        console.log('🔄 当前 analysisStatus:', analysisStatus.value)
        if (status.status === 'pending' || status.status === 'running') {
          analysisStatus.value = 'running'
          console.log('✅ 设置 analysisStatus 为 running')
          updateProgressInfo(status)
        }
      } catch (error) {
        console.error('立即查询状态失败:', error)
      }
    }, 1000) // 1秒后查询

  } catch (error: any) {
    ElMessage.error(error.message || '提交分析失败')
  } finally {
    submitting.value = false
  }
}

// 轮询任务状态
const startPollingTaskStatus = () => {
  if (pollingTimer.value) {
    clearInterval(pollingTimer.value)
  }

  // 检查任务ID是否有效
  if (!currentTaskId.value) {
    console.error('❌ 任务ID为空，无法开始轮询')
    return
  }

  console.log('🔄 开始轮询任务状态:', currentTaskId.value)

  pollingTimer.value = setInterval(async () => {
    try {
      if (!currentTaskId.value) {
        console.error('❌ 轮询中任务ID为空')
        if (pollingTimer.value) {
          clearInterval(pollingTimer.value)
        }
        return
      }

      console.log('🔄 开始查询任务状态:', currentTaskId.value)
      const response = await analysisApi.getTaskStatus(currentTaskId.value)
      const status = response.data // 响应拦截器已返回 response.data

      console.log('🔍 任务状态响应:', response)
      console.log('🔍 任务状态数据:', status)
      console.log('🔍 当前状态:', status.status, '进度:', status.progress)

      if (status.status === 'completed') {
        if (pollingTimer.value) {
          clearInterval(pollingTimer.value)
          pollingTimer.value = null
        }

        // 分析完成，调用专门的结果API获取完整数据
        console.log('🎉 分析完成，正在获取完整结果...')

        try {
          const finalResult = await loadCompletedAnalysisResult(currentTaskId.value, status)
          applyAnalysisResult(finalResult)
          console.log('✅ 获取完整分析结果成功:', finalResult)

          // 添加调试信息
          console.log('🔍 完整结果数据结构:', {
            hasDecision: !!finalResult?.decision,
            hasState: !!finalResult?.state,
            hasReports: !!finalResult?.reports,
            hasSummary: !!finalResult?.summary,
            hasRecommendation: !!finalResult?.recommendation,
            keys: Object.keys(finalResult || {})
          })
        } catch (error) {
          console.error('❌ 获取分析结果异常:', error)
          applyAnalysisResult(status.result_data || null)
        }

        analysisStatus.value = 'completed'
        showResults.value = true
        progressInfo.value.progress = 100
        progressInfo.value.currentStep = '分析完成'
        progressInfo.value.message = '分析已完成！'

        // 任务完成后保持缓存，以便刷新后能看到结果
        // clearTaskCache() // 不清除，让用户能在30分钟内刷新查看结果

        ElMessage.success('分析完成！')

      } else if (status.status === 'failed') {
        // 分析失败
        analysisStatus.value = 'failed'
        progressInfo.value.currentStep = '分析失败'

        // 格式化错误消息（保留换行符）
        const errorMessage = status.error_message || '分析过程中发生错误'
        progressInfo.value.message = errorMessage

        if (pollingTimer.value) {
          clearInterval(pollingTimer.value)
          pollingTimer.value = null
        }

        // 任务失败时清除缓存
        clearTaskCache()

        // 显示友好的错误提示（使用 dangerouslyUseHTMLString 支持换行）
        ElMessage({
          type: 'error',
          message: errorMessage.replace(/\n/g, '<br>'),
          dangerouslyUseHTMLString: true,
          duration: 10000, // 显示10秒，让用户有时间阅读
          showClose: true
        })

      } else if (status.status === 'pending' || status.status === 'running') {
        // 分析进行中，更新进度
        console.log('🔄 轮询中设置 analysisStatus 为 running')
        analysisStatus.value = 'running'
        updateProgressInfo(status)
      }

    } catch (error) {
      console.error('获取任务状态失败:', error)
      // 继续轮询，不中断
    }
  }, 5000) // 每5秒轮询一次
}

// 更新进度信息
const updateProgressInfo = (status: any) => {
  console.log('🔄 更新进度信息:', status)
  console.log('🔄 当前进度信息:', progressInfo.value)

  // 使用后端返回的实际进度数据
  if (status.progress !== undefined) {
    console.log('📊 更新进度:', status.progress)
    progressInfo.value.progress = status.progress
  }

  if (status.current_step_name) {
    console.log('📋 更新步骤:', status.current_step_name)
    progressInfo.value.currentStep = status.current_step_name
  }

  if (status.current_step_description) {
    console.log('📝 更新步骤描述:', status.current_step_description)
    progressInfo.value.currentStepDescription = status.current_step_description
  }

  if (status.message) {
    console.log('💬 更新消息:', status.message)
    progressInfo.value.message = status.message
  }

  // 接收后端返回的时间数据
  if (status.elapsed_time !== undefined) {
    progressInfo.value.elapsedTime = status.elapsed_time
  }

  if (status.remaining_time !== undefined) {
    progressInfo.value.remainingTime = status.remaining_time
  }

  if (status.estimated_total_time !== undefined) {
    progressInfo.value.totalTime = status.estimated_total_time
  }

  // 如果后端提供了步骤数据，更新步骤列表
  if (status.steps && Array.isArray(status.steps)) {
    if (analysisSteps.value.length === 0) {
      // 首次生成步骤列表
      analysisSteps.value = generateStepsFromBackend(status.steps)
      console.log('📋 从后端生成步骤列表:', analysisSteps.value.length, '个步骤')
    }
  }

  console.log('🔄 更新后进度信息:', progressInfo.value)

  // 更新分析步骤状态
  updateAnalysisSteps(status)

  // 前端不进行估算，只展示后端返回的数据
  progressInfo.value.message = status.message || '分析正在进行中...'
}

// 重新开始分析
const restartAnalysis = () => {
  // 清除任务缓存
  clearTaskCache()

  analysisStatus.value = 'idle'
  showResults.value = false
  analysisResults.value = null
  currentTaskId.value = ''
  progressInfo.value = {
    progress: 0,
    currentStep: '',
    currentStepDescription: '',
    message: '',
    elapsedTime: 0,
    remainingTime: 0,
    totalTime: 0
  }

  if (pollingTimer.value) {
    clearInterval(pollingTimer.value)
    pollingTimer.value = null
  }
}

// 开始新分析（保留当前分析在后台运行）
const startNewAnalysis = () => {
  // 停止当前任务的轮询（但不清除任务ID，让后台继续运行）
  if (pollingTimer.value) {
    clearInterval(pollingTimer.value)
    pollingTimer.value = null
  }

  // 重置分析状态和结果，但不清除任务ID（让用户可以在任务中心查看）
  analysisStatus.value = 'idle'
  showResults.value = false
  analysisResults.value = null
  
  // 重置进度信息
  progressInfo.value = {
    progress: 0,
    currentStep: '',
    currentStepDescription: '',
    message: '',
    elapsedTime: 0,
    remainingTime: 0,
    totalTime: 0
  }

  // 清空股票代码，让用户输入新的股票代码
  analysisForm.stockCode = ''
  
  // 滚动到顶部，方便用户输入新的股票代码
  window.scrollTo({ top: 0, behavior: 'smooth' })
  
  ElMessage.success('可以开始分析新的股票了，之前的分析任务仍在后台进行中')
}


const analysisModuleMeta: Record<string, { title: string; description: string }> = {
  index_report: { title: '📊 大盘环境研究', description: '大盘指数走势、市场环境与系统性风险研究' },
  sector_report: { title: '🏭 行业板块研究', description: '行业趋势、板块轮动与同业比较研究' },
  market_report: { title: '📈 市场技术研究', description: '技术指标、趋势结构与关键位置研究' },
  fundamentals_report: { title: '💰 基本面研究', description: '财务质量、估值水平与盈利能力研究' },
  sentiment_report: { title: '💭 情绪研究', description: '市场热度、投资者情绪与讨论偏差研究' },
  news_report: { title: '📰 新闻研究', description: '公告、新闻事件与催化影响研究' },
  bull_researcher: { title: '🐂 乐观情景研究员', description: '围绕结论上修空间的研究论证' },
  bull_report: { title: '🐂 乐观情景研究员', description: '围绕结论上修空间的研究论证' },
  bear_researcher: { title: '🐻 审慎情景研究员', description: '围绕脆弱前提与下修风险的研究论证' },
  bear_report: { title: '🐻 审慎情景研究员', description: '围绕脆弱前提与下修风险的研究论证' },
  research_team_decision: { title: '🔬 平衡研究结论', description: '综合乐观与审慎材料后的研究层结论' },
  investment_plan: { title: '📋 研究经理简报', description: '研究经理对多维分析的整合摘要' },
  trader_investment_plan: { title: '🧩 研究简报', description: '面向用户的综合研究简报与阅读入口' },
  risky_analyst: { title: '🔥 高弹性情景观点', description: '结论上修条件与强化催化研究' },
  risky_opinion: { title: '🔥 高弹性情景观点', description: '结论上修条件与强化催化研究' },
  safe_analyst: { title: '🛡️ 防御情景观点', description: '脆弱前提、关键风险与下修条件研究' },
  safe_opinion: { title: '🛡️ 防御情景观点', description: '脆弱前提、关键风险与下修条件研究' },
  neutral_analyst: { title: '⚖️ 基准情景观点', description: '基准情景、证据质量与信息缺口研究' },
  neutral_opinion: { title: '⚖️ 基准情景观点', description: '基准情景、证据质量与信息缺口研究' },
  risk_management_decision: { title: '⚠️ 风险审阅结论', description: '综合情景审阅后的最终稳健性结论' },
  risk_assessment: { title: '⚠️ 风险审阅结论', description: '综合情景审阅后的最终稳健性结论' },
  final_trade_decision: { title: '📝 综合研究结论', description: '融合研究结论与风险审阅后的总览结论' },
}

const getResearchContentFingerprint = (moduleData: any) => {
  const prepared = prepareMarkdownContent(getModuleContent(moduleData) || '')
  if (!prepared) {
    return ''
  }

  return prepared
    .replace(/```[\s\S]*?```/g, '')
    .replace(/^#{1,6}\s+/gm, '')
    .replace(/^[-*+]\s+/gm, '')
    .replace(/^\d+[.)]\s+/gm, '')
    .replace(/\*\*(.*?)\*\*/g, '$1')
    .replace(/__(.*?)__/g, '$1')
    .replace(/`([^`]*)`/g, '$1')
    .replace(/\[(.*?)\]\([^)]*\)/g, '$1')
    .replace(/[>_*|~]/g, ' ')
    .replace(/[\s\u3000]+/g, '')
}

const shouldHideDuplicateResearchManagerBrief = (moduleKey: string, reportsData: any) => {
  if (moduleKey !== 'investment_plan') {
    return false
  }

  const researchDecision = getResearchContentFingerprint(reportsData?.research_team_decision)
  const managerBrief = getResearchContentFingerprint(reportsData?.investment_plan)

  return !!researchDecision && researchDecision === managerBrief
}

const extractReportsSource = (data: any): Record<string, any> | null => {
  if (data?.reports && typeof data.reports === 'object' && Object.keys(data.reports).length > 0) {
    return data.reports
  }

  if (data?.state && typeof data.state === 'object' && Object.keys(data.state).length > 0) {
    return data.state
  }

  return null
}

const normalizeDisplayedResearchText = (content: string) => {
  if (!content) return ''

  const replacements: Array<[RegExp, string]> = [
    [/交易分析计划/g, '研究简报'],
    [/交易员计划/g, '研究简报'],
    [/初步投资计划/g, '平衡研究结论'],
    [/投资计划/g, '研究结论'],
    [/投资建议/g, '研究结论'],
    [/操作建议/g, '研究观点'],
    [/大盘指数分析/g, '大盘环境研究'],
    [/行业板块分析/g, '行业板块研究'],
    [/市场情绪分析/g, '情绪研究'],
    [/新闻事件分析/g, '新闻研究'],
    [/风险管理决策/g, '风险审阅结论'],
    [/风险审阅摘要/g, '风险审阅结论'],
    [/综合研究简报/g, '综合研究结论'],
    [/最终分析结果/g, '综合研究结论'],
    [/激进风险分析师/g, '高弹性情景分析师'],
    [/保守风险分析师/g, '防御情景分析师'],
    [/中性风险分析师/g, '基准情景分析师'],
  ]

  return replacements.reduce((text, [pattern, replacement]) => text.replace(pattern, replacement), content)
}

/**
 * 将风险审阅 JSON 对象渲染为可读的 Markdown 文本。
 * 字段与后端 risk_assessment / risk_management_decision 结构保持一致。
 */
const formatRiskAssessment = (value: any): string | null => {
  if (!value || typeof value !== 'object') return null
  const riskLevel = value.risk_level || value.riskLevel
  const riskScore = value.risk_score ?? value.riskScore
  const reasoning = value.reasoning || value.conclusion || value.summary || value.assessment
  const keyRisks = value.key_risks || value.keyRisks || value.risks
  const riskControl = value.risk_control || value.riskControl || value.control || value.suggestions
  const investmentAdjustment = value.investment_adjustment || value.investmentAdjustment || value.adjustment

  if (riskLevel === undefined && riskScore === undefined && !reasoning && !riskControl && !investmentAdjustment) {
    return null
  }

  const parts: string[] = []
  if (riskLevel !== undefined) {
    parts.push(`**风险等级**：${riskLevel}`)
  }
  if (riskScore !== undefined) {
    const score = typeof riskScore === 'number' ? riskScore.toFixed(2) : riskScore
    parts.push(`**风险评分**：${score}`)
  }
  if (reasoning) {
    parts.push(`**评估结论**：${reasoning}`)
  }
  if (Array.isArray(keyRisks) && keyRisks.length > 0) {
    parts.push(`**核心风险**：`)
    keyRisks.forEach((risk: any) => {
      if (risk) parts.push(`- ${String(risk).trim()}`)
    })
  }
  if (riskControl) {
    parts.push(`**风控建议**：${riskControl}`)
  }
  if (investmentAdjustment) {
    parts.push(`**投资调整建议**：${investmentAdjustment}`)
  }

  return parts.join('\n\n')
}

const _stripJsonCodeFence = (text: string): string => {
  // 去除 ```json ... ``` 或 ``` ... ``` 代码块包裹
  let t = text.trim()
  const fenceMatch = t.match(/^```(?:json|JSON)?\s*\n([\s\S]*?)\n```\s*$/i)
  if (fenceMatch) {
    t = fenceMatch[1].trim()
  }
  return t
}

const _looksLikeRiskJson = (text: string): boolean => {
  const lowered = text.toLowerCase()
  return (
    text.startsWith('{') &&
    (lowered.includes('"risk_level"') ||
      lowered.includes('"risk_score"') ||
      lowered.includes('"reasoning"') ||
      lowered.includes('"key_risks"') ||
      lowered.includes('"risk_control"') ||
      lowered.includes('"investment_adjustment"'))
  )
}

const extractStructuredText = (value: any): string | null => {
  if (!value) return null

  if (typeof value === 'string') {
    // 先剥离 ```json 代码块，再判断是否为风险审阅 JSON
    const stripped = _stripJsonCodeFence(value.trim())
    if (!stripped) return null

    if (_looksLikeRiskJson(stripped)) {
      try {
        const parsed = JSON.parse(stripped)
        const riskText = formatRiskAssessment(parsed)
        if (riskText) return riskText
      } catch {
        // 不是合法 JSON，按普通文本返回
      }
    }
    // 如果原文是 ```json 代码块但不是风险 JSON，返回剥离后的内容
    return stripped !== value.trim() ? stripped : value.trim()
  }

  if (typeof value === 'object') {
    // 风险审阅结构化对象直接渲染为可读文本
    const riskText = formatRiskAssessment(value)
    if (riskText) return riskText

    // 对象里嵌套 content/text 字段时，递归尝试解析
    // 真实数据结构: { content: "```json\n{...}\n```", success: true }
    for (const key of ['content', 'markdown', 'text', 'message', 'report', 'judge_decision', 'risk_assessment', 'risk_management_decision']) {
      const item = value[key]
      if (item) {
        const nested = extractStructuredText(item)
        if (nested) return nested
      }
    }
  }

  return null
}

const prepareMarkdownContent = (content: string) => {
  let preparedText = normalizeDisplayedResearchText(content || '').trim()

  if (
    ((preparedText.startsWith('"') && preparedText.endsWith('"')) ||
      (preparedText.startsWith("'") && preparedText.endsWith("'"))) &&
    preparedText.length >= 2
  ) {
    preparedText = preparedText.slice(1, -1).trim()
  }

  const fencedMarkdownMatch = preparedText.match(/^```(?:markdown|md)?\s*\n([\s\S]*?)\n```\s*$/i)
  if (fencedMarkdownMatch) {
    preparedText = fencedMarkdownMatch[1].trim()
  }

  return preparedText
}

const renderMarkdown = (content: string) => {
  if (!content) return ''

  try {
    return marked.parse(prepareMarkdownContent(content)) as string
  } catch {
    return `<pre style="white-space: pre-wrap; font-family: inherit;">${prepareMarkdownContent(content)}</pre>`
  }
}

const getModuleContent = (moduleData: any): string | null => {
  const extractedText = extractStructuredText(moduleData)
  if (extractedText) {
    return normalizeDisplayedResearchText(extractedText)
  }

  if (moduleData && typeof moduleData !== 'string') {
    return null
  }

  return typeof moduleData === 'string' ? normalizeDisplayedResearchText(moduleData) : null
}

const getOverviewBriefContent = (data: any): string | null => {
  const reportsData = extractReportsSource(data)
  const traderBrief = getModuleContent(reportsData?.trader_investment_plan)
  if (traderBrief) {
    return traderBrief
  }

  return getModuleContent(data?.summary) || getModuleContent(data?.recommendation)
}

const overviewBriefContent = computed(() => getOverviewBriefContent(analysisResults.value))

// 获取分析报告
const getAnalysisReports = (data: any) => {
  const reports: Array<{field: string, title: string, content: any}> = []

  const reportsData = extractReportsSource(data)
  if (!reportsData) {
    return reports
  }

  const isEtfAnalysis = data?.task_type === 'etf_analysis' || (Array.isArray(data?.analysts) && data.analysts.includes('etf_analyst'))
  const addedKeys = new Set<string>()

  const pushReport = (key: string, title: string, content: any) => {
    if (!content || addedKeys.has(key)) {
      return
    }
    addedKeys.add(key)
    reports.push({ field: key, title, content })
  }

  // 动态模式：使用 report_manifest（自定义工作流产出物描述）
  const manifest = data?.report_manifest as Array<{
    field: string; display_name: string; icon?: string; description?: string; category: string; order: number; is_primary: boolean
  }> | undefined
  analysisReportManifest.value = manifest || []
  if (reportsData && manifest && manifest.length > 0) {
    const sorted = [...manifest].sort((a, b) => a.order - b.order)
    for (const m of sorted) {
      const content = reportsData[m.field]
      if (content) {
        pushReport(m.field, m.display_name || m.field.replace(/_/g, ' '), content)
      }
    }
  }

  // --- 旧逻辑：内置工作流使用硬编码映射 ---
  const reportMappings = [
    { key: 'index_report', title: '📊 大盘环境研究' },
    { key: 'sector_report', title: '🏭 行业板块研究' },
    { key: 'market_report', title: '📈 市场技术研究' },
    { key: 'fundamentals_report', title: isEtfAnalysis ? '📊 ETF 分析' : '💰 基本面研究' },
    { key: 'sentiment_report', title: '💭 情绪研究' },
    { key: 'news_report', title: '📰 新闻研究' },
    { key: 'chip_report', title: '🧮 筹码分布研究' },
    // 工坊 Agent 新增
    { key: 'valuation_report', title: '📊 多维度估值分析' },
    // 研究团队（去重：优先用带后缀的）
    { key: 'bull_report', title: '🐂 乐观情景研究员' },
    { key: 'bear_report', title: '🐻 审慎情景研究员' },
    { key: 'research_team_decision', title: '🔬 平衡研究结论' },
    { key: 'investment_plan', title: '📋 研究经理简报' },
    { key: 'trader_investment_plan', title: '🧩 研究简报' },
    // 风险团队（去重：优先用带后缀的）
    { key: 'risky_opinion', title: '🔥 高弹性情景观点' },
    { key: 'safe_opinion', title: '🛡️ 防御情景观点' },
    { key: 'neutral_opinion', title: '⚖️ 基准情景观点' },
    { key: 'risk_assessment', title: '⚠️ 风险审阅结论' },
    { key: 'final_trade_decision', title: '📝 综合研究结论' },
  ]

  reportMappings.forEach(mapping => {
    if (addedKeys.has(mapping.key)) return
    if (shouldHideDuplicateResearchManagerBrief(mapping.key, reportsData)) {
      addedKeys.add(mapping.key)  // 标记为已处理，避免兜底遍历重复添加
      return
    }
    const content = reportsData?.[mapping.key]
    if (content) {
      pushReport(mapping.key, mapping.title, content)
    }
  })

  Object.keys(reportsData).forEach(key => {
    if (key === 'structured_reports' || addedKeys.has(key)) {
      return
    }

    // 跳过重复的 key（避免 researcher/analyst 后缀的重复项）
    const skipPatterns = [
      // 研究团队：跳过 _researcher 后缀（优先用 _report）
      /^bull_researcher$/,
      /^bear_researcher$/,
      // 风险团队：跳过 _analyst 后缀（优先用 _opinion）
      /^risky_analyst$/,
      /^safe_analyst$/,
      /^neutral_analyst$/,
      /^risk_management_decision$/, // 跳过这个（优先用 risk_assessment）
    ]
    if (skipPatterns.some(p => p.test(key))) {
      addedKeys.add(key)
      return
    }

    const content = reportsData[key]
    if (content) {
      pushReport(key, key.replace(/_/g, ' '), content)
    }
  })

  console.log(`📊 总共找到 ${reports.length} 个报告`)

  // 设置第一个报告为默认激活标签页
  if (reports.length > 0 && !activeReportTab.value) {
    activeReportTab.value = '0'
  }

  return reports
}

// 获取报告图标
const getReportIcon = (field: string, title: string) => {
  // 1. 优先从 manifest 获取 icon
  const manifestEntry = analysisReportManifest.value.find((m) => m.field === field)
  if (manifestEntry?.icon) {
    return manifestEntry.icon
  }
  // 2. 回退到硬编码 iconMap
  const iconMap: Record<string, string> = {
    // 🆕 宏观分析师图标
    '📊 大盘指数分析': '📊',
    '🏭 行业板块分析': '🏭',
    // 分析师团队图标
    '📈 市场技术分析': '📈',
    '💰 基本面分析': '💰',
    '📊 ETF 分析': '📊',
    '📰 新闻事件分析': '📰',
    '💭 市场情绪分析': '💭',
    '💡 初步研究观察': '💡',
    '🔬 研究团队分析': '🔬',
    '🐂 积极证据研究': '🐂',
    '🐻 谨慎证据研究': '🐻',
    '💼 研究简报': '💼',
    '⚖️ 风险管理团队': '⚖️',
    '🔥 高弹性情景研究观察': '🔥',
    '🛡️ 防御情景研究观察': '🛡️',
    '⚖️ 基准情景研究观察': '⚖️',
    '⚠️ 风险评估': '⚠️',
    '🎯 最终分析结果': '🎯'
  }
  return iconMap[title] || '📊'
}

// 获取报告名称（去掉图标前缀）
const getReportName = (title: string) => {
  return title.replace(/^[^\s]+\s/, '')
}

// 获取报告描述
const getReportDescription = (field: string, title: string) => {
  // 1. 优先从 manifest 获取 description
  const manifestEntry = analysisReportManifest.value.find((m) => m.field === field)
  if (manifestEntry?.description) {
    return manifestEntry.description
  }
  // 2. 回退到硬编码 analysisModuleMeta
  const plainTitle = title.replace(/^[^\s]+\s/, '')
  return Object.values(analysisModuleMeta).find(meta => meta.title === title)?.description
    || Object.values(analysisModuleMeta).find(meta => meta.title.endsWith(plainTitle))?.description
    || '详细研究报告'
}

// 格式化报告内容
const formatReportContent = (content: any) => {
  const normalizedContent = getModuleContent(content)
  return normalizedContent ? renderMarkdown(normalizedContent) : ''
}

// 下载报告
const downloadReport = async (format: string = 'markdown') => {
  try {
    if (!analysisResults.value && !currentTaskId.value) {
      ElMessage.error('报告尚未生成，无法下载')
      return
    }

    // 显示加载提示
    const loadingMsg = ElMessage({
      message: `正在生成${getFormatName(format)}格式报告...`,
      type: 'info',
      duration: 0
    })

    const reportId = (analysisResults.value?.id as any) || currentTaskId.value
    const res: any = await ApiClient.get(`/api/reports/${reportId}/download`, { format }, { responseType: 'blob' })

    loadingMsg.close()

    // 响应拦截器直接返回 Blob（response.data）；兼容返回 { data: Blob } 的旧实现
    const blob = res instanceof Blob ? res : (res?.data instanceof Blob ? res.data : new Blob([res]))
    const url = window.URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    const code =
      analysisResults.value?.stock_code ||
      analysisResults.value?.stock_symbol ||
      analysisResults.value?.symbol ||
      'stock'
    const dateStr = analysisResults.value?.analysis_date || new Date().toISOString().slice(0, 10)

    // 根据格式设置文件扩展名
    const ext = getFileExtension(format)
    a.download = `${String(code)}_分析报告_${String(dateStr).slice(0, 10)}.${ext}`

    document.body.appendChild(a)
    a.click()
    window.URL.revokeObjectURL(url)
    document.body.removeChild(a)

    ElMessage.success(`${getFormatName(format)}报告下载成功`)
  } catch (err: any) {
    console.error('下载报告出错:', err)

    // 显示详细错误信息
    if (err.message && err.message.includes('pandoc')) {
      ElMessage.error({
        message: 'Word 导出需要安装 pandoc 工具（PDF 导出不需要 pandoc）',
        duration: 5000
      })
    } else if (err.message && (err.message.includes('weasyprint') || err.message.includes('pdfkit'))) {
      ElMessage.error({
        message: 'PDF 导出需要安装 PDF 引擎：pip install weasyprint（推荐）',
        duration: 5000
      })
    } else {
      ElMessage.error(`下载报告失败: ${err.message || '未知错误'}`)
    }
  }
}

// 辅助函数：获取格式名称
const getFormatName = (format: string): string => {
  const names: Record<string, string> = {
    'markdown': 'Markdown',
    'docx': 'Word',
    'pdf': 'PDF',
    'json': 'JSON'
  }
  return names[format] || format
}

// 辅助函数：获取文件扩展名
const getFileExtension = (format: string): string => {
  const extensions: Record<string, string> = {
    'markdown': 'md',
    'docx': 'docx',
    'pdf': 'pdf',
    'json': 'json'
  }
  return extensions[format] || 'txt'
}

// 组件销毁时清理定时器
onUnmounted(() => {
  if (pollingTimer.value) {
    clearInterval(pollingTimer.value)
    pollingTimer.value = null
  }
  clearSyncPolling()
})

// 页面可见性变化时的处理
const handleVisibilityChange = () => {
  if (document.hidden) {
    console.log('📱 页面隐藏，暂停轮询')
  } else {
    console.log('📱 页面显示，恢复轮询')
    // 页面重新可见时，立即查询一次状态
    if (currentTaskId.value && analysisStatus.value === 'running') {
      setTimeout(async () => {
        try {
          const response = await analysisApi.getTaskStatus(currentTaskId.value)
          const status = response.data // 响应拦截器已返回 response.data
          console.log('🔄 页面恢复查询状态:', status)
          if (status.status === 'pending' || status.status === 'running') {
            analysisStatus.value = 'running'
            updateProgressInfo(status)
          }
        } catch (error) {
          console.error('页面恢复查询状态失败:', error)
        }
      }, 500)
    }
  }
}

// 监听页面可见性变化
document.addEventListener('visibilitychange', handleVisibilityChange)

// 获取深度描述
const getDepthDescription = (depth: number) => {
  const descriptions = ['快速', '基础', '标准', '深度', '全面']
  return descriptions[depth - 1] || '标准'
}

// 获取进度条状态
const getProgressStatus = () => {
  if (analysisStatus.value === 'completed') {
    return 'success'
  } else if (analysisStatus.value === 'failed') {
    return 'exception'
  } else if (analysisStatus.value === 'running') {
    return '' // 默认状态，显示蓝色进度条
  }
  return ''
}

// 简单的时间格式化方法（只用于显示后端返回的时间）
const formatTime = (seconds: number) => {
  if (!seconds || seconds <= 0) {
    return '计算中...'
  }

  if (seconds < 60) {
    return `${Math.floor(seconds)}秒`
  } else if (seconds < 3600) {
    const minutes = Math.floor(seconds / 60)
    const remainingSeconds = Math.floor(seconds % 60)
    return remainingSeconds > 0 ? `${minutes}分${remainingSeconds}秒` : `${minutes}分钟`
  } else {
    const hours = Math.floor(seconds / 3600)
    const minutes = Math.floor((seconds % 3600) / 60)
    return `${hours}小时${minutes}分钟`
  }
}

// 更新分析步骤状态
const updateAnalysisSteps = (status: any) => {
  console.log('📋 步骤更新输入:', status)

  if (analysisSteps.value.length === 0) {
    console.log('📋 没有步骤定义，跳过更新')
    return
  }

  // 优先使用后端提供的详细步骤信息
  let currentStepIndex = 0

  if (status.current_step !== undefined) {
    // 后端提供了精确的步骤索引
    currentStepIndex = status.current_step
    console.log('📋 使用后端步骤索引:', currentStepIndex)
  } else {
    // 兜底方案：使用进度百分比估算
    const progress = status.progress_percentage || status.progress || 0
    if (progress > 0) {
      const progressRatio = progress / 100
      currentStepIndex = Math.floor(progressRatio * (analysisSteps.value.length - 1))
      if (progress > 0 && currentStepIndex === 0) {
        currentStepIndex = 1
      }
    }
    console.log('📋 使用进度估算步骤索引:', currentStepIndex, '进度:', progress)
  }

  // 确保索引在有效范围内
  currentStepIndex = Math.max(0, Math.min(currentStepIndex, analysisSteps.value.length - 1))

  console.log('📋 最终步骤索引:', currentStepIndex, '/', analysisSteps.value.length)

  // 更新所有步骤状态
  analysisSteps.value.forEach((step, index) => {
    if (index < currentStepIndex) {
      step.status = 'completed'
    } else if (index === currentStepIndex) {
      step.status = 'current'
    } else {
      step.status = 'pending'
    }
  })

  const statusSummary = analysisSteps.value.map((s, i) => `${i}:${s.status}`).join(', ')
  console.log('📋 步骤状态更新完成:', statusSummary)
}

watch(() => [modelSettings.value.quickAnalysisModel, modelSettings.value.deepAnalysisModel], names => {
  modelsReady.value = names.every(name => availableModels.value.some(model => model.model_name === name || `${model.provider}::${model.model_name}` === name))
  if (modelsReady.value) modelLoadError.value = ''
})

// 初始化模型设置
const initializeModelSettings = async () => {
  modelsReady.value = false
  modelLoadError.value = ''
  try {
    // 获取默认模型
    const defaultModels = await configApi.getDefaultModels()
    modelSettings.value.quickAnalysisModel = defaultModels.quick_analysis_model
    modelSettings.value.deepAnalysisModel = defaultModels.deep_analysis_model

    // 获取所有可用的模型列表
    const llmConfigs = await configApi.getLLMConfigs()
    availableModels.value = llmConfigs.filter((config: any) => config.enabled)
    modelsReady.value = [modelSettings.value.quickAnalysisModel, modelSettings.value.deepAnalysisModel].every(
      name => availableModels.value.some(model => model.model_name === name || `${model.provider}::${model.model_name}` === name)
    )
    if (!modelsReady.value) modelLoadError.value = '默认研究模型尚未配置或已停用，请在下方设置 Astra，或在高级选项中选择模型。'

    console.log('✅ 加载模型配置成功:', {
      quick: modelSettings.value.quickAnalysisModel,
      deep: modelSettings.value.deepAnalysisModel,
      available: availableModels.value.length
    })
    console.log('🔍 可用模型详细信息:', availableModels.value.map(m => ({
      model_name: m.model_name,
      model_display_name: m.model_display_name,
      provider: m.provider
    })))
  } catch (error) {
    console.error('加载默认模型配置失败:', error)
    modelLoadError.value = '无法加载研究模型，请检查后端连接后刷新页面。'
  }
}

// 任务状态缓存管理
const TASK_CACHE_KEY = 'trading_analysis_task'
const TASK_CACHE_DURATION = 30 * 60 * 1000 // 30分钟

// 保存任务状态到缓存
const saveTaskToCache = (taskId: string, taskData: any) => {
  const cacheData = {
    taskId,
    taskData,
    timestamp: Date.now()
  }
  localStorage.setItem(TASK_CACHE_KEY, JSON.stringify(cacheData))
  console.log('💾 任务状态已缓存:', taskId)
}

// 从缓存获取任务状态
const getTaskFromCache = () => {
  try {
    const cached = localStorage.getItem(TASK_CACHE_KEY)
    if (!cached) return null

    const cacheData = JSON.parse(cached)
    const now = Date.now()

    // 检查是否过期（30分钟）
    if (now - cacheData.timestamp > TASK_CACHE_DURATION) {
      localStorage.removeItem(TASK_CACHE_KEY)
      console.log('🗑️ 缓存已过期，已清理')
      return null
    }

    console.log('📦 从缓存恢复任务:', cacheData.taskId)
    return cacheData
  } catch (error) {
    console.error('❌ 读取缓存失败:', error)
    localStorage.removeItem(TASK_CACHE_KEY)
    return null
  }
}

// 清除任务缓存
const clearTaskCache = () => {
  localStorage.removeItem(TASK_CACHE_KEY)
  console.log('🗑️ 任务缓存已清除')
}

// 恢复任务状态
const restoreTaskFromCache = async () => {
  const cached = getTaskFromCache()
  if (!cached) return false

  try {
    console.log('🔄 尝试恢复任务状态:', cached.taskId)

    // 查询任务当前状态
    const response = await analysisApi.getTaskStatus(cached.taskId)
    const status = response.data // 响应拦截器已返回 response.data

    console.log('📊 恢复的任务状态:', status)

    if (status.status === 'completed') {
      // 任务已完成，显示结果
      currentTaskId.value = cached.taskId
      analysisStatus.value = 'completed'
      showResults.value = true
      applyAnalysisResult(await loadCompletedAnalysisResult(cached.taskId, status))
      progressInfo.value.progress = 100
      progressInfo.value.currentStep = '分析完成'
      progressInfo.value.message = '分析已完成'

      // 恢复分析参数
      if (cached.taskData.parameters) {
        Object.assign(analysisForm, cached.taskData.parameters)
      }

      console.log('✅ 任务已完成，显示结果')
      return true

    } else if (status.status === 'pending' || status.status === 'running') {
      // 任务仍在运行，恢复进度显示
      currentTaskId.value = cached.taskId
      analysisStatus.value = 'running'
      showResults.value = false
      updateProgressInfo(status)

      // 恢复分析参数
      if (cached.taskData.parameters) {
        Object.assign(analysisForm, cached.taskData.parameters)
      }

      // 启动轮询
      startPollingTaskStatus()

      console.log('🔄 任务仍在运行，恢复进度显示')
      return true

    } else if (status.status === 'failed') {
      // 任务失败
      analysisStatus.value = 'failed'
      progressInfo.value.currentStep = '分析失败'
      progressInfo.value.message = status.error_message || '分析过程中发生错误'

      // 清除缓存
      clearTaskCache()

      console.log('❌ 任务失败')
      return true

    } else {
      // 其他状态，清除缓存
      clearTaskCache()
      console.log('🤔 未知任务状态，清除缓存')
      return false
    }

  } catch (error) {
    console.error('❌ 恢复任务状态失败:', error)
    // 如果查询失败，可能是任务不存在了，清除缓存
    clearTaskCache()
    return false
  }
}

// 🆕 模型能力相关辅助函数

/**
 * 获取能力等级文本
 */
const getCapabilityText = (level: number): string => {
  const texts: Record<number, string> = {
    1: '⚡基础',
    2: '📊标准',
    3: '🎯高级',
    4: '🔥专业',
    5: '👑旗舰'
  }
  return texts[level] || '📊标准'
}

/**
 * 获取能力等级标签类型
 */
const getCapabilityTagType = (level: number): 'success' | 'info' | 'warning' | 'danger' => {
  if (level >= 4) return 'danger'
  if (level >= 3) return 'warning'
  if (level >= 2) return 'success'
  return 'info'
}

/**
 * 判断是否适合快速分析
 */
const isQuickAnalysisRole = (roles: string[] | undefined): boolean => {
  if (!roles || !Array.isArray(roles)) return false
  return roles.includes('quick_analysis') || roles.includes('both')
}

/**
 * 判断是否适合深度分析
 */
/**
 * 显示分析深度的模型推荐说明
 */
const checkModelSuitability = async () => {
  const depthNames: Record<number, string> = {
    1: '快速',
    2: '基础',
    3: '标准',
    4: '深度',
    5: '全面'
  }
  const depthName = depthNames[analysisForm.researchDepth] || '标准'

  try {
    // 获取推荐模型
    const recommendRes = await recommendModels(depthName)
    const responseData = recommendRes?.data?.data

    if (responseData) {
      const quickModel = responseData.quick_model || '未知'
      const deepModel = responseData.deep_model || '未知'

      // 获取模型的显示名称
      const quickModelInfo = availableModels.value.find(m => m.model_name === quickModel)
      const deepModelInfo = availableModels.value.find(m => m.model_name === deepModel)

      const quickDisplayName = quickModelInfo?.model_display_name || quickModel
      const deepDisplayName = deepModelInfo?.model_display_name || deepModel

      // 获取推荐理由
      const reason = responseData.reason || ''

      // 构建推荐说明
      const depthDescriptions: Record<number, string> = {
        1: '快速浏览，获取基本信息',
        2: '基础分析，了解主要指标',
        3: '标准分析，全面评估股票',
        4: '深度研究，挖掘投资机会',
        5: '全面分析，专业投资决策'
      }

      const message = `${depthDescriptions[analysisForm.researchDepth] || '标准分析'}\n\n推荐模型配置：\n• 快速模型：${quickDisplayName}\n• 深度模型：${deepDisplayName}\n\n${reason}`

      modelRecommendation.value = {
        title: '💡 模型推荐',
        message,
        type: 'info',
        quickModel,
        deepModel
      }
    } else {
      // 如果没有推荐数据，显示通用说明
      const generalDescriptions: Record<number, string> = {
        1: '快速分析：使用基础模型即可，注重速度和成本',
        2: '基础分析：快速模型用基础级，深度模型用标准级',
        3: '标准分析：快速模型用基础级，深度模型用标准级以上',
        4: '深度分析：快速模型用标准级，深度模型用高级以上，需要推理能力',
        5: '全面分析：快速模型用标准级，深度模型用专业级以上，强推理能力'
      }

      modelRecommendation.value = {
        title: '💡 模型推荐',
        message: generalDescriptions[analysisForm.researchDepth] || generalDescriptions[3],
        type: 'info'
      }
    }
  } catch (error) {
    console.error('获取模型推荐失败:', error)
    // 显示通用说明
    const generalDescriptions: Record<number, string> = {
      1: '快速分析：使用基础模型即可，注重速度和成本',
      2: '基础分析：快速模型用基础级，深度模型用标准级',
      3: '标准分析：快速模型用基础级，深度模型用标准级以上',
      4: '深度分析：快速模型用标准级，深度模型用高级以上，需要推理能力',
      5: '全面分析：快速模型用标准级，深度模型用专业级以上，强推理能力'
    }

    modelRecommendation.value = {
      title: '💡 模型推荐',
      message: generalDescriptions[analysisForm.researchDepth] || generalDescriptions[3],
      type: 'info'
    }
  }
}

// 应用推荐的模型配置
const applyRecommendedModels = () => {
  if (modelRecommendation.value?.quickModel && modelRecommendation.value?.deepModel) {
    modelSettings.value.quickAnalysisModel = modelRecommendation.value.quickModel
    modelSettings.value.deepAnalysisModel = modelRecommendation.value.deepModel

    // 清除推荐提示
    modelRecommendation.value = null

    ElMessage.success('已应用推荐的模型配置')
  }
}

// 监听分析深度变化
watch(() => analysisForm.researchDepth, () => {
  checkModelSuitability()
})

// 监听模型选择变化
watch([() => modelSettings.value.quickAnalysisModel, () => modelSettings.value.deepAnalysisModel], () => {
  checkModelSuitability()
})

// 页面初始化
onMounted(async () => {
  initializeModelSettings()

  // 🆕 从用户偏好加载默认设置
  const authStore = useAuthStore()
  const appStore = useAppStore()

  // 优先从 authStore.user.preferences 读取，其次从 appStore.preferences 读取
  const userPrefs = authStore.user?.preferences
  if (userPrefs) {
    // 加载默认市场
    if (userPrefs.default_market) {
      analysisForm.market = userPrefs.default_market as MarketType
    }

    // 加载默认分析深度（转换为数字）
    if (userPrefs.default_depth) {
      analysisForm.researchDepth = parseInt(userPrefs.default_depth)
    }

    // 加载默认分析师
    if (userPrefs.default_analysts && userPrefs.default_analysts.length > 0) {
      analysisForm.selectedAnalysts = normalizeSelectedAnalystsForMarket(
        analysisForm.market,
        [...userPrefs.default_analysts]
      )
    }

    console.log('✅ 已加载用户偏好设置:', {
      market: analysisForm.market,
      depth: analysisForm.researchDepth,
      analysts: analysisForm.selectedAnalysts
    })
  } else {
    // 降级到 appStore.preferences
    if (appStore.preferences.defaultMarket) {
      analysisForm.market = appStore.preferences.defaultMarket as MarketType
    }
    if (appStore.preferences.defaultDepth) {
      analysisForm.researchDepth = parseInt(appStore.preferences.defaultDepth)
    }
    console.log('✅ 已加载应用偏好设置（降级）')
  }

  // 接收一次路由参数（从筛选页带入）- 路由参数优先级最高
  const q = route.query as any
  const hasNewStock = !!q?.stock
  if (hasNewStock) {
    analysisForm.stockCode = String(q.stock)
    // 🔥 关键修复：如果有新的股票代码，清除旧任务缓存
    clearTaskCache()
    console.log('🔄 检测到新股票代码，已清除旧任务缓存:', q.stock)

    // 🆕 自动识别市场类型（如果URL中没有明确指定market参数）
    if (!q?.market) {
      const detectedMarket = getMarketByStockCode(analysisForm.stockCode)
      analysisForm.market = detectedMarket as MarketType
      if (detectedMarket === 'ETF') {
        analysisForm.selectedAnalysts = [...ETF_ANALYST_NAMES]
      } else {
        analysisForm.selectedAnalysts = normalizeSelectedAnalystsForMarket(
          detectedMarket as MarketType,
          analysisForm.selectedAnalysts
        )
      }
      console.log('🔍 自动识别市场类型:', analysisForm.stockCode, '->', detectedMarket)
    }
  }
  if (q?.market) {
    analysisForm.market = normalizeMarketForAnalysis(q.market) as MarketType
    // 🔥 如果从路由参数设置市场类型，也需要同步分析师
    if (analysisForm.market === 'ETF') {
      analysisForm.selectedAnalysts = [...ETF_ANALYST_NAMES]
    } else {
      analysisForm.selectedAnalysts = normalizeSelectedAnalystsForMarket(
        analysisForm.market,
        analysisForm.selectedAnalysts
      )
    }
  }

  if (analysisForm.market === 'A股' && analysisForm.stockCode) {
    await fetchFactorPreview(analysisForm.stockCode)
  }

  // 尝试恢复任务状态（仅当没有新股票代码时）
  if (!hasNewStock) {
    await restoreTaskFromCache()
  }

  // 🆕 初始检查模型适用性
  await checkModelSuitability()
  try {
    workflows.value = await workflowApi.listAll()
    console.log('[SingleAnalysis] 加载的所有流程:', workflows.value)
    console.log('[SingleAnalysis] 过滤后的流程:', workflowsSorted.value)
    
    // 优先选择标记为 is_default 的流程
    const defaultWorkflow = workflowsSorted.value.find(w => w.is_default)
    if (defaultWorkflow) {
      selectedWorkflowId.value = defaultWorkflow.id
      console.log('[SingleAnalysis] 选中默认流程:', defaultWorkflow.id, defaultWorkflow.name)
    } else if (workflowsSorted.value.length > 0) {
      // 如果没有标记默认，选择第一个
      selectedWorkflowId.value = workflowsSorted.value[0].id
      console.log('[SingleAnalysis] 没有默认流程，选中第一个:', workflowsSorted.value[0].id, workflowsSorted.value[0].name)
    }
  } catch (error) {
    console.debug('[SingleAnalysis] 加载工作流失败，保留当前默认模板:', error)
  }
})


  return {
  router,
  route,
  submitting,
  validatingData,
  showValidationDialog,
  validationResult,
  factorPreviewLoading,
  factorPreview,
  syncStatus,
  syncPollingTimer,
  currentTaskId,
  analysisStatus,
  showResults,
  analysisResults,
  analysisReportManifest,
  activeReportTab,
  growthTaskId,
  progressInfo,
  pollingTimer,
  applyAnalysisResult,
  loadCompletedAnalysisResult,
  analysisSteps,
  generateStepsFromBackend,
  showAdvanced,
  modelsReady,
  modelLoadError,
  formatResearchDate,
  stockSearchVersion,
  searchStockSuggestions,
  selectStockSuggestion,
  inferMarketFromCode,
  modelSettings,
  availableModels,
  workflows,
  workflowsSorted,
  parseVersion,
  selectedWorkflowId,
  modelRecommendation,
  analysisForm,
  stockCodeError,
  stockCodeHelp,
  depthOptions,
  disabledDate,
  factorPreviewRow,
  clearFactorPreview,
  fetchFactorPreview,
  onStockCodeInput,
  ETF_ANALYST_NAMES,
  normalizeSelectedAnalystsForMarket,
  availableAnalysts,
  onMarketChange,
  validateStockCodeInput,
  fetchStockInfo,
  toggleAnalyst,
  handleValidateData,
  sleep,
  isTerminalStockSyncStatus,
  buildSingleSyncTaskMessage,
  STOCK_SYNC_POLL_MAX_ATTEMPTS,
  STOCK_SYNC_POLL_INTERVAL_MS,
  waitForStockSyncTask,
  monitorSingleSyncTask,
  handleSyncMissingData,
  clearSyncPolling,
  showValidationResultDialog,
  formatValidationDetails,
  submitAnalysis,
  startPollingTaskStatus,
  updateProgressInfo,
  restartAnalysis,
  startNewAnalysis,
  analysisModuleMeta,
  getResearchContentFingerprint,
  shouldHideDuplicateResearchManagerBrief,
  extractReportsSource,
  normalizeDisplayedResearchText,
  formatRiskAssessment,
  _stripJsonCodeFence,
  _looksLikeRiskJson,
  extractStructuredText,
  prepareMarkdownContent,
  renderMarkdown,
  getModuleContent,
  getOverviewBriefContent,
  overviewBriefContent,
  getAnalysisReports,
  getReportIcon,
  getReportName,
  getReportDescription,
  formatReportContent,
  downloadReport,
  getFormatName,
  getFileExtension,
  handleVisibilityChange,
  getDepthDescription,
  getProgressStatus,
  formatTime,
  updateAnalysisSteps,
  initializeModelSettings,
  TASK_CACHE_KEY,
  TASK_CACHE_DURATION,
  saveTaskToCache,
  getTaskFromCache,
  clearTaskCache,
  restoreTaskFromCache,
  getCapabilityText,
  getCapabilityTagType,
  isQuickAnalysisRole,
  checkModelSuitability,
  applyRecommendedModels,
  ResearchDefaults,
  DeepModelSelector,
  TaskGrowthPanel,
  Document,
  TrendCharts,
  InfoFilled,
  Check,
  Loading,
  Refresh,
  Download,
  WarningFilled,
  Cpu,
  Plus,
  DocumentChecked,
  View,
  }
}
