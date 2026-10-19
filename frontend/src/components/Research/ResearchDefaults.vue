<template>
  <el-card v-loading="loading" shadow="never" class="research-defaults">
    <div class="connection-header">
      <div>
        <strong>本机 Astra</strong>
        <p v-if="error" role="alert">{{ error }}</p>
        <p v-else-if="!astra">尚未发现已启用的 Astra 配置。请确认本机已安装 Codex 并登录，再重启后端。</p>
        <p v-else>{{ isDefault ? '已用于默认股票分析和研究助手' : '使用本机 ChatGPT 登录，可统一用于股票分析和研究助手。' }}</p>
        <p v-if="testMessage" role="status">{{ testMessage }}</p>
        <p v-if="!compact" class="connection-note">连接测试会实际调用 Astra，消耗套餐额度。模型已注册不代表登录或额度当前可用。</p>
      </div>
      <div class="connection-actions">
        <el-button v-if="error || !astra" @click="load">重新检测</el-button>
        <el-button v-if="astra && !compact" :loading="testing" :disabled="saving" @click="testConnection">测试连接</el-button>
        <el-button v-if="astra && !isDefault" type="primary" :loading="saving" :disabled="testing" @click="setDefault">设为研究默认模型</el-button>
        <el-tag v-if="isDefault" type="success">默认模型</el-tag>
        <el-button v-if="compact" text @click="$router.push('/settings/config')">模型设置</el-button>
      </div>
    </div>
  </el-card>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { configApi, type LLMConfig } from '@/api/config'

defineProps<{ compact?: boolean }>()
const emit = defineEmits<{ saved: [] }>()
const astra = ref<LLMConfig>()
const defaults = ref<Record<string, any>>({})
const loading = ref(false)
const saving = ref(false)
const testing = ref(false)
const error = ref('')
const testMessage = ref('')
const roles = ['quick_analysis_model', 'deep_analysis_model', 'deep_reasoning_model']
const isDefault = computed(() => !!astra.value && roles.every(key => {
  const id = defaults.value[`${key}_config_id`]
  return id ? id === astra.value?.config_id : defaults.value[key] === astra.value?.model_name && defaults.value.default_provider === 'codex'
}))

const load = async () => {
  loading.value = true
  error.value = ''
  try {
    const [models, settings, providers] = await Promise.all([
      configApi.getLLMConfigs(), configApi.getDefaultModels(), configApi.getLLMProviders()
    ])
    const provider = providers.find(p => p.name === 'codex' && p.is_active)
    astra.value = provider ? models.find(m => m.provider === 'codex' && m.model_name === 'gpt-6-astra' && m.enabled) : undefined
    defaults.value = settings
  } catch {
    astra.value = undefined
    defaults.value = {}
    error.value = '无法读取模型配置，请检查后端和数据库连接。'
  } finally {
    loading.value = false
  }
}
const setDefault = async () => {
  if (!astra.value) return
  saving.value = true
  try {
    const payload: Record<string, any> = { default_provider: 'codex' }
    for (const key of roles) {
      payload[key] = astra.value.model_name
      if (astra.value.config_id) payload[`${key}_config_id`] = astra.value.config_id
    }
    const result = await configApi.updateSystemSettings(payload)
    if (result.success === false) throw new Error(result.message)
    await load()
    if (!isDefault.value || error.value) throw new Error('默认模型未保存，请检查系统设置权限。')
    ElMessage.success('Astra 已设为研究默认模型')
    emit('saved')
  } catch (err: any) {
    ElMessage.error(err.message || '保存失败，请重试')
  } finally {
    saving.value = false
  }
}
const testConnection = async () => {
  if (!astra.value) return
  testing.value = true
  testMessage.value = ''
  try {
    const result = await configApi.testConfig({ config_type: 'llm', config_data: astra.value })
    testMessage.value = result.message
    if (!result.success) ElMessage.error(result.message)
  } catch (err: any) {
    testMessage.value = err.message || '连接失败，请检查本机 Codex 登录和网络。'
  } finally {
    testing.value = false
  }
}
onMounted(load)
</script>

<style scoped>
.research-defaults { margin-bottom: 20px; }
.connection-header { display: flex; align-items: center; flex-wrap: wrap; justify-content: space-between; gap: 12px; }
p { color: var(--el-text-color-secondary); font-size: 13px; line-height: 1.6; margin: 6px 0 0; }
.connection-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.connection-actions .el-button { margin-left: 0; }
.connection-note { max-width: 650px; }
</style>
