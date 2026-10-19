<template>
  <div class="single-analysis">
    <!-- 页面头部 -->
    <div class="page-header">
      <div class="header-content">
        <div class="title-section">
          <h1 class="page-title">
            <el-icon class="title-icon"><Document /></el-icon>
            {{ analysisForm.market === 'ETF' ? 'ETF分析' : '单股分析' }}
          </h1>
          
          <!-- 风险提示 -->
          <div class="risk-disclaimer">
            <el-alert
              type="warning"
              :closable="false"
              show-icon
            >
              <template #title>
                <span style="font-size: 14px;">
                  <strong>⚠️ 重要提示：</strong>本次分析所有分析结论用于学习和验证AI证券分析技术，不作为真实交易操盘指导。
                </span>
              </template>
            </el-alert>
          </div>
          
          <p class="page-description">
            AI驱动的智能证券分析，多维度评估投资价值与风险
          </p>
        </div>
      </div>
    </div>

    <!-- 主要分析表单 -->
    <div class="analysis-container">
      <el-row :gutter="24">
        <!-- 左侧：基础配置 -->
        <el-col :xs="24" :lg="showAdvanced ? 18 : 24">
          <el-card class="main-form-card" shadow="hover">
            <template #header>
              <div class="card-header">
                <h3>开始研究</h3>
                <el-button text @click="showAdvanced = !showAdvanced" :aria-expanded="showAdvanced">
                  {{ showAdvanced ? '收起高级选项' : '高级选项' }}
                </el-button>
              </div>
            </template>

            <el-form :model="analysisForm" label-width="100px" class="analysis-form">
              <!-- 标的信息 -->
              <div class="form-section">
                <h4 class="section-title">📊 {{ analysisForm.market === 'ETF' ? 'ETF信息' : '股票信息' }}</h4>
                <el-row :gutter="16">
                  <el-col :span="showAdvanced ? 12 : 24">
                    <el-form-item :label="analysisForm.market === 'ETF' ? 'ETF' : '股票'" required>
                      <el-autocomplete
                        :fetch-suggestions="searchStockSuggestions"
                        :trigger-on-focus="false"
                        @select="selectStockSuggestion"
                        v-model="analysisForm.stockCode"
                        placeholder="输入股票名称或代码，如宁德时代、300750"
                        clearable
                        size="large"
                        class="stock-input"
                        :class="{ 'is-error': stockCodeError }"
                        @input="onStockCodeInput"
                      >
                        <template #prefix><el-icon><TrendCharts /></el-icon></template>
                        <template #default="{ item }">{{ item.label }}</template>
                      </el-autocomplete>
                      <div v-if="stockCodeError" class="error-message">
                        <el-icon><WarningFilled /></el-icon>
                        {{ stockCodeError }}
                      </div>
                      <div v-else-if="stockCodeHelp" class="help-message">
                        <el-icon><InfoFilled /></el-icon>
                        {{ stockCodeHelp }}
                      </div>
                    </el-form-item>
                  </el-col>
                  <el-col v-show="showAdvanced" :span="12">
                    <el-form-item label="市场类型">
                      <el-select
                        v-model="analysisForm.market"
                        placeholder="选择市场"
                        size="large"
                        style="width: 100%"
                        @change="onMarketChange"
                      >
                        <el-option label="🇨🇳 A股市场" value="A股">
                          <span>🇨🇳 A股市场</span>
                          <span style="color: #909399; font-size: 12px; margin-left: 8px;">（6位数字）</span>
                        </el-option>
                        <el-option label="📈 ETF 基金" value="ETF">
                          <span>📈 ETF 基金</span>
                          <span style="color: #909399; font-size: 12px; margin-left: 8px;">（510050、159919 等）</span>
                        </el-option>
                      </el-select>
                    </el-form-item>
                  </el-col>
                </el-row>

                <el-form-item v-show="showAdvanced" label="分析日期">
                  <div style="display: flex; align-items: center; gap: 8px; width: 100%">
                    <el-date-picker
                      v-model="analysisForm.analysisDate"
                      type="date"
                      placeholder="选择分析基准日期"
                      size="large"
                      style="flex: 1"
                      :disabled-date="disabledDate"
                    />
                    <el-button
                      type="primary"
                      size="large"
                      :loading="validatingData"
                      @click="handleValidateData"
                      :disabled="!analysisForm.stockCode.trim()"
                      style="white-space: nowrap"
                    >
                      <el-icon><DocumentChecked /></el-icon>
                      数据检验
                    </el-button>
                  </div>
                </el-form-item>
              </div>

              <!-- 分析深度 -->
              <div v-show="showAdvanced" class="form-section">
                <h4 class="section-title">🎯 分析深度</h4>
                <div class="depth-selector">
                  <div
                    v-for="(depth, index) in depthOptions"
                    :key="index"
                    class="depth-option"
                    :class="{ active: analysisForm.researchDepth === index + 1 }"
                    @click="analysisForm.researchDepth = index + 1"
                  >
                    <div class="depth-icon">{{ depth.icon }}</div>
                    <div class="depth-info">
                      <div class="depth-name">{{ depth.name }}</div>
                      <div class="depth-desc">{{ depth.description }}</div>
                      <div class="depth-time">{{ depth.time }}</div>
                    </div>
                  </div>
                </div>
              </div>

              <!-- 分析师团队 -->
              <div v-show="showAdvanced" class="form-section">
                <h4 class="section-title">👥 分析师团队</h4>
                <div class="analysts-grid">
                  <div
                    v-for="analyst in availableAnalysts"
                    :key="analyst.id"
                    class="analyst-card"
                    :class="{ 
                      active: analysisForm.selectedAnalysts.includes(analyst.name),
                      disabled: analyst.disabled
                    }"
                    @click="!analyst.disabled && toggleAnalyst(analyst.name)"
                  >
                    <div class="analyst-avatar">
                      <el-icon>
                        <component :is="analyst.icon" />
                      </el-icon>
                    </div>
                    <div class="analyst-content">
                      <div class="analyst-name">
                        {{ analyst.name }}
                        <el-tag v-if="analyst.disabled" size="small" type="info" style="margin-left: 8px;">
                          仅A股
                        </el-tag>
                      </div>
                      <div class="analyst-desc">{{ analyst.description }}</div>
                    </div>
                    <div class="analyst-check">
                      <el-icon v-if="analysisForm.selectedAnalysts.includes(analyst.name)" class="check-icon">
                        <Check />
                      </el-icon>
                    </div>
                  </div>
                </div>
              </div>

              <el-alert v-if="modelLoadError" :title="modelLoadError" type="error" :closable="false" />
              <div class="research-summary">
                <span>{{ getDepthDescription(analysisForm.researchDepth) }}分析 · {{ analysisForm.selectedAnalysts.length }} 个分析维度 · {{ modelSettings.quickAnalysisModel === modelSettings.deepAnalysisModel ? (availableModels.find(model => model.model_name === modelSettings.quickAnalysisModel)?.model_display_name || modelSettings.quickAnalysisModel) : '组合模型' }}</span>
                <span>数据截至 {{ formatResearchDate(analysisForm.analysisDate) }}</span>
              </div>
              <ResearchDefaults v-if="!showAdvanced" compact @saved="initializeModelSettings" />
              <!-- 操作按钮 -->
              <div class="form-section">
                <!-- 执行轨迹开关 -->
                <div v-show="showAdvanced" style="display: flex; justify-content: center; margin-bottom: 12px;">
                  <el-tooltip
                    content="开启后，系统会记录每个 Agent 的完整执行过程（Prompt全文、LLM原始返回、校验结果），用于排查分析问题。数据量较大，平时不需要时建议关闭。"
                    placement="top"
                  >
                    <el-checkbox v-model="analysisForm.enableTrace" size="small">
                      <el-icon style="margin-right: 4px;"><View /></el-icon>
                      记录执行轨迹（排查问题时开启）
                    </el-checkbox>
                  </el-tooltip>
                </div>

                <div class="action-buttons" style="display: flex; justify-content: center; align-items: center; width: 100%; text-align: center;">
                  <el-button
                    v-if="analysisStatus === 'idle'"
                    type="primary"
                    size="large"
                    @click="submitAnalysis"
                    :loading="submitting"
                    :disabled="!analysisForm.stockCode.trim() || !modelsReady"
                    class="submit-btn large-analysis-btn"
                    style="width: 280px; height: 56px; font-size: 18px; font-weight: 700; border-radius: 16px;"
                  >
                    <el-icon><TrendCharts /></el-icon>
                    开始智能分析
                  </el-button>

                  <el-button
                    v-else-if="analysisStatus === 'running'"
                    type="warning"
                    size="large"
                    disabled
                    class="submit-btn large-analysis-btn"
                    style="width: 280px; height: 56px; font-size: 18px; font-weight: 700; border-radius: 16px;"
                  >
                    <el-icon><Loading /></el-icon>
                    分析进行中...
                  </el-button>

                  <div v-else-if="analysisStatus === 'completed'" style="display: flex; gap: 12px;">
                    <el-button
                      type="success"
                      size="large"
                      @click="showResults = !showResults"
                      class="submit-btn"
                      style="width: 180px; height: 56px; font-size: 16px; font-weight: 700; border-radius: 16px;"
                    >
                      <el-icon><Document /></el-icon>
                      {{ showResults ? '隐藏结果' : '查看结果' }}
                    </el-button>

                    <el-button
                      type="primary"
                      size="large"
                      @click="restartAnalysis"
                      class="submit-btn"
                      style="width: 180px; height: 56px; font-size: 16px; font-weight: 700; border-radius: 16px;"
                    >
                      <el-icon><Refresh /></el-icon>
                      重新分析
                    </el-button>
                  </div>

                  <el-button
                    v-else-if="analysisStatus === 'failed'"
                    type="danger"
                    size="large"
                    @click="restartAnalysis"
                    class="submit-btn large-analysis-btn"
                    style="width: 280px; height: 56px; font-size: 18px; font-weight: 700; border-radius: 16px;"
                  >
                    <el-icon><Refresh /></el-icon>
                    重新分析
                  </el-button>
                </div>
              </div>

              <!-- 分析进度显示 -->
              <div v-if="analysisStatus === 'running'" class="progress-section">
                <el-card class="progress-card" shadow="hover">
                  <template #header>
                    <div class="progress-header">
                      <h4>
                        <el-icon class="rotating-icon">
                          <Loading />
                        </el-icon>
                        分析进行中...
                      </h4>
                      <el-button
                        type="primary"
                        size="small"
                        @click="startNewAnalysis"
                        style="margin-left: auto;"
                      >
                        <el-icon><Plus /></el-icon>
                        开始新分析
                      </el-button>
                      <!-- 任务ID已隐藏 -->
                      <!-- <el-tag type="warning">{{ currentTaskId }}</el-tag> -->
                    </div>
                  </template>

                  <div class="progress-content">
                    <!-- 总体进度信息 -->
                    <div class="overall-progress-info">
                      <div class="progress-stats">
                        <!-- 当前步骤已隐藏 -->
                        <!--
                        <div class="stat-item">
                          <div class="stat-label">当前步骤</div>
                          <div class="stat-value">{{ progressInfo.currentStep || '初始化中...' }}</div>
                        </div>
                        -->
                        <!-- 整体进度已隐藏 -->
                        <!--
                        <div class="stat-item">
                          <div class="stat-label">整体进度</div>
                          <div class="stat-value">{{ progressInfo.progress.toFixed(1) }}%</div>
                        </div>
                        -->
                        <div class="stat-item">
                          <div class="stat-label">已用时间</div>
                          <div class="stat-value">{{ formatTime(progressInfo.elapsedTime) }}</div>
                        </div>
                        <div class="stat-item">
                          <div class="stat-label">预计剩余</div>
                          <div class="stat-value">{{ formatTime(progressInfo.remainingTime) }}</div>
                        </div>
                        <div class="stat-item">
                          <div class="stat-label">预计总时长</div>
                          <div class="stat-value">{{ formatTime(progressInfo.totalTime) }}</div>
                        </div>
                      </div>
                    </div>

                    <!-- 进度条 -->
                    <div class="progress-bar-section">
                      <el-progress
                        :percentage="Math.round(progressInfo.progress)"
                        :stroke-width="12"
                        :show-text="true"
                        :status="getProgressStatus()"
                        class="main-progress-bar"
                      />
                    </div>

                    <!-- 当前任务详情 -->
                    <div class="current-task-info">
                      <div class="task-title">
                        <el-icon class="task-icon">
                          <Loading />
                        </el-icon>
                        {{ progressInfo.currentStep || '正在初始化分析引擎...' }}
                      </div>
                      <div
                        class="task-description"
                        style="white-space: pre-wrap; line-height: 1.6;"
                      >
                        {{ progressInfo.currentStepDescription || progressInfo.message || 'AI正在根据您的要求重点分析相关内容' }}
                      </div>
                    </div>

                    <!-- 分析步骤显示 - 已隐藏 -->
                    <!--
                    <div v-if="analysisSteps.length > 0" class="analysis-steps">
                      <h5 class="steps-title">📋 分析步骤</h5>
                      <div class="steps-container">
                        <div
                          v-for="(step, index) in analysisSteps"
                          :key="index"
                          class="step-item"
                          :class="{
                            'step-completed': step.status === 'completed',
                            'step-current': step.status === 'current',
                            'step-pending': step.status === 'pending'
                          }"
                        >
                          <div class="step-icon">
                            <el-icon v-if="step.status === 'completed'" class="completed-icon">
                              <Check />
                            </el-icon>
                            <el-icon v-else-if="step.status === 'current'" class="current-icon rotating-icon">
                              <Loading />
                            </el-icon>
                            <el-icon v-else class="pending-icon">
                              <Clock />
                            </el-icon>
                          </div>
                          <div class="step-content">
                            <div class="step-title">{{ step.title }}</div>
                            <div class="step-description">{{ step.description }}</div>
                          </div>
                        </div>
                      </div>
                    </div>
                    -->
                  </div>
                </el-card>
              </div>
            </el-form>
          </el-card>
        </el-col>

        <!-- 右侧：高级配置 -->
        <el-col v-show="showAdvanced" :xs="24" :lg="6">
          <el-card class="config-card" shadow="hover">
            <template #header>
              <div class="card-header">
                <h3>高级配置</h3>
                <el-tag type="warning" size="small">可选设置</el-tag>
              </div>
            </template>

            <div class="config-content">
              <!-- AI模型配置 -->
              <div class="config-section">
                <h4 class="config-title">🤖 AI模型配置</h4>
                <div class="model-config">
                  <div class="model-item">
                    <div class="model-label">
                      <span>快速分析模型</span>
                      <el-tooltip content="用于市场分析、新闻分析、基本面分析等" placement="top">
                        <el-icon class="help-icon"><InfoFilled /></el-icon>
                      </el-tooltip>
                    </div>
                    <el-select v-model="modelSettings.quickAnalysisModel" size="small" style="width: 100%" filterable>
                      <el-option
                        v-for="model in availableModels"
                        :key="`quick-${model.provider}/${model.model_name}`"
                        :label="model.model_display_name || model.model_name"
                        :value="model.model_name"
                      >
                        <div style="display: flex; justify-content: space-between; align-items: center; gap: 8px;">
                          <span style="flex: 1;">{{ model.model_display_name || model.model_name }}</span>
                          <div style="display: flex; align-items: center; gap: 4px;">
                            <!-- 能力等级徽章 -->
                            <el-tag
                              v-if="model.capability_level"
                              :type="getCapabilityTagType(model.capability_level)"
                              size="small"
                              effect="plain"
                            >
                              {{ getCapabilityText(model.capability_level) }}
                            </el-tag>
                            <!-- 角色标签 -->
                            <el-tag
                              v-if="isQuickAnalysisRole(model.suitable_roles)"
                              type="success"
                              size="small"
                              effect="plain"
                            >
                              ⚡快速
                            </el-tag>
                            <span style="font-size: 12px; color: #909399;">{{ model.provider }}</span>
                          </div>
                        </div>
                      </el-option>
                    </el-select>
                  </div>

                  <div class="model-item">
                    <div class="model-label">
                      <span>深度决策模型</span>
                      <el-tooltip content="用于研究整合员综合决策、风险评估师最终评估" placement="top">
                        <el-icon class="help-icon"><InfoFilled /></el-icon>
                      </el-tooltip>
                    </div>
                    <DeepModelSelector v-model="modelSettings.deepAnalysisModel" :available-models="availableModels" type="deep" size="small" width="100%" />
                  </div>
                </div>

                <!-- 🆕 模型推荐提示 -->
                <el-alert
                  v-if="modelRecommendation"
                  :title="modelRecommendation.title"
                  :type="modelRecommendation.type"
                  :closable="false"
                  style="margin-top: 12px;"
                >
                  <template #default>
                    <div style="display: flex; justify-content: space-between; align-items: flex-start; gap: 12px;">
                      <div style="font-size: 13px; line-height: 1.8; flex: 1; white-space: pre-line;">
                        {{ modelRecommendation.message }}
                      </div>
                      <el-button
                        v-if="modelRecommendation.quickModel && modelRecommendation.deepModel"
                        type="primary"
                        size="small"
                        @click="applyRecommendedModels"
                        style="flex-shrink: 0;"
                      >
                        应用推荐
                      </el-button>
                    </div>
                  </template>
                </el-alert>
              </div>

              <!--  流程选择器 -->
              <div class="config-section" v-if="workflowsSorted.length > 0">
                <h4 class="config-title">📋 分析流程</h4>
                <el-select
                  :key="`workflow-select-${analysisForm.market}`"
                  v-model="selectedWorkflowId"
                  size="small"
                  style="width: 100%"
                  placeholder="选择分析流程"
                >
                  <el-option
                    v-for="workflow in workflowsSorted"
                    :key="workflow.id"
                    :label="`${workflow.name} v${workflow.version}`"
                    :value="workflow.id"
                  />
                </el-select>
                <el-tooltip content="选择不同的分析流程，系统会使用该流程对应的智能体和步骤进行分析" placement="top">
                  <el-icon style="margin-left: 8px; color: #909399; cursor: help;"><InfoFilled /></el-icon>
                </el-tooltip>
              </div>

              <!-- 分析选项 - 已隐藏，流程内置 -->
              <!--
              <div class="config-section">
                <h4 class="config-title">⚙️ 分析选项</h4>
                <div class="option-list">
                  <div class="option-item">
                    <div class="option-info">
                      <span class="option-name">情绪分析</span>
                      <span class="option-desc">分析市场情绪和投资者心理</span>
                    </div>
                    <el-switch v-model="analysisForm.includeSentiment" />
                  </div>

                  <div class="option-item">
                    <div class="option-info">
                      <span class="option-name">风险评估</span>
                      <span class="option-desc">包含详细的风险因素分析</span>
                    </div>
                    <el-switch v-model="analysisForm.includeRisk" />
                  </div>

                  <div class="option-item">
                    <div class="option-info">
                      <span class="option-name">语言偏好</span>
                    </div>
                    <el-select v-model="analysisForm.language" size="small" style="width: 100px">
                      <el-option label="中文" value="zh-CN" />
                      <el-option label="English" value="en-US" />
                    </el-select>
                  </div>
                </div>
              </div>
              -->

            </div>
          </el-card>
        </el-col>
      </el-row>

      <!-- 分析结果显示 -->
      <div v-if="showResults && analysisResults" class="results-section">
        <el-row :gutter="24">
          <el-col :span="24">
            <el-card class="results-card" shadow="hover">
              <template #header>
                <div class="results-header">
                  <h3>📊 分析结果</h3>
                  <div class="result-meta">
                    <el-tag type="success">{{ analysisResults.symbol || analysisResults.stock_symbol || analysisForm.symbol || analysisForm.stockCode }}</el-tag>
                    <el-tag>{{ analysisResults.analysis_date }}</el-tag>
                    <el-tag v-if="analysisResults.model_info && analysisResults.model_info !== 'Unknown'" type="info">
                      <el-icon><Cpu /></el-icon>
                      {{ analysisResults.model_info }}
                    </el-tag>
                  </div>
                </div>
              </template>

              <div class="results-content">
                <!-- 风险提示 -->
                <div class="risk-disclaimer">
                  <el-alert
                    type="warning"
                    :closable="false"
                    show-icon
                  >
                    <template #title>
                      <div class="disclaimer-content">
                        <el-icon class="disclaimer-icon"><WarningFilled /></el-icon>
                        <div class="disclaimer-text">
                          <p style="margin: 0 0 8px 0;"><strong>⚠️ 重要风险提示与免责声明</strong></p>
                          <ul style="margin: 0; padding-left: 20px; line-height: 1.8;">
                            <li><strong>平台性质：</strong>本平台为AI辅助分析技术学习平台，专注于AI技术在证券与ETF分析领域的应用验证和技术学习。</li>
                            <li><strong>分析目的：</strong>所有分析结论仅用于学习和验证AI辅助分析技术，不作为真实交易操盘指导或投资依据。</li>
                            <li><strong>非投资建议：</strong>所有分析结果、评分、建议仅为技术验证参考，不构成任何买卖建议或投资决策依据。</li>
                            <li><strong>数据局限性：</strong>分析基于历史数据和公开信息，可能存在延迟、不完整或不准确的情况，无法预测未来市场走势。</li>
                            <li><strong>投资风险：</strong>股票与ETF投资均存在市场风险、流动性风险、政策风险等多种风险，可能导致本金损失。</li>
                            <li><strong>独立决策：</strong>投资者应基于自身风险承受能力、投资目标和财务状况独立做出投资决策。</li>
                            <li><strong>责任声明：</strong>使用本平台产生的任何投资决策及其后果由使用者自行承担，本平台不承担任何责任。</li>
                          </ul>
                        </div>
                      </div>
                    </template>
                  </el-alert>
                </div>

                <!-- 降级节点警告 -->
                <el-alert
                  v-if="analysisResults.degraded_nodes && Object.keys(analysisResults.degraded_nodes).length > 0"
                  type="warning"
                  :closable="false"
                  show-icon
                  style="margin-bottom: 16px"
                >
                  <template #title>部分分析节点执行失败（已降级）</template>
                  <div style="margin-top: 4px; font-size: 13px; line-height: 1.6">
                    <div v-for="(info, nodeId) in analysisResults.degraded_nodes" :key="nodeId">
                      <strong>{{ info.label || nodeId }}</strong>：{{ info.error ? info.error.substring(0, 120) : '未知错误' }}
                    </div>
                    <div style="margin-top: 4px; color: #909399">其余节点的分析结果不受影响。</div>
                  </div>
                </el-alert>

                <!-- 分析概览 -->
                <div v-if="overviewBriefContent" class="overview-section">
                  <h4>📊 分析概览</h4>
                  <div class="overview-card">
                    <div class="overview-summary">
                      <div class="markdown-content" v-html="renderMarkdown(overviewBriefContent)"></div>
                    </div>

                    <!-- v2.1.0 结果页以研究简报为主入口，不再在这里重复堆叠另一张决策卡 -->
                  </div>
                </div>

                <TaskGrowthPanel
                  v-if="growthTaskId"
                  class="growth-panel-section"
                  :task-id="growthTaskId"
                />

                <!-- 详细分析报告 -->
                <div v-if="analysisResults.state || analysisResults.reports" class="reports-section">
                  <h4>📋 详细分析报告</h4>

                  <!-- 美观的标签页展示 -->
                  <div class="analysis-tabs-container">
                    <el-tabs
                      v-model="activeReportTab"
                      type="card"
                      class="analysis-tabs"
                      tab-position="top"
                      :key="analysisResults?.id || 'default'"
                    >
                      <el-tab-pane
                        v-for="(report, key) in getAnalysisReports(analysisResults)"
                        :key="key"
                        :name="key.toString()"
                        :label="report.title"
                        class="report-tab-pane"
                      >
                        <!-- 标签页内容头部 -->
                        <div class="report-header">
                          <div class="report-title">
                            <span class="report-icon">{{ getReportIcon(report.field, report.title) }}</span>
                            <span class="report-name">{{ getReportName(report.title) }}</span>
                          </div>
                          <div class="report-description">{{ getReportDescription(report.field, report.title) }}</div>
                        </div>

                        <!-- 报告内容 -->
                        <div class="report-content-wrapper">
                          <!-- AI分析免责声明 -->
                          <el-alert
                            type="info"
                            :closable="false"
                            show-icon
                            style="margin-bottom: 16px;"
                          >
                            <template #title>
                              <span style="font-size: 13px;">
                                ⚠️ 重要提示：本报告由AI基于有限资料分析生成，仅供参考，不构成投资建议。投资有风险，决策需谨慎。
                              </span>
                            </template>
                          </el-alert>
                          
                          <div
                            class="report-content"
                            v-html="formatReportContent(report.content)"
                            v-if="report.content"
                          ></div>
                          <div v-else class="no-content">
                            <el-empty description="暂无内容" />
                          </div>
                        </div>
                      </el-tab-pane>
                    </el-tabs>
                  </div>
                </div>

                <!-- 操作按钮 -->
                <div class="result-actions">
                  <el-dropdown trigger="click" @command="downloadReport">
                    <el-button type="primary">
                      <el-icon><Download /></el-icon>
                      下载报告
                      <el-icon class="el-icon--right"><arrow-down /></el-icon>
                    </el-button>
                    <template #dropdown>
                      <el-dropdown-menu>
                        <el-dropdown-item command="markdown">
                          <el-icon><document /></el-icon> Markdown
                        </el-dropdown-item>
                        <el-dropdown-item command="docx">
                          <el-icon><document /></el-icon> Word 文档
                        </el-dropdown-item>
                        <el-dropdown-item command="pdf">
                          <el-icon><document /></el-icon> PDF
                        </el-dropdown-item>
                        <el-dropdown-item command="json" divided>
                          <el-icon><document /></el-icon> JSON (原始数据)
                        </el-dropdown-item>
                      </el-dropdown-menu>
                    </template>
                  </el-dropdown>
                </div>

                <!-- 风险提示 -->
                <el-alert
                  type="warning"
                  :closable="false"
                  show-icon
                  class="risk-disclaimer"
                >
                  <template #title>
                    <span style="font-weight: bold;">报告依据真实交易数据使用AI分析生成，仅供参考，不构成任何投资建议。市场有风险，投资需谨慎。</span>
                  </template>
                </el-alert>
              </div>
            </el-card>
          </el-col>
        </el-row>
      </div>
    </div>

    <!-- 数据校验结果对话框 -->
    <el-dialog
      v-model="showValidationDialog"
      title="数据检验结果"
      width="800px"
      :close-on-click-modal="false"
    >
      <div v-if="validationResult" class="validation-result">
        <!-- 总体状态 -->
        <el-alert
          :type="validationResult.is_valid ? 'success' : 'warning'"
          :closable="false"
          show-icon
          style="margin-bottom: 20px"
        >
          <template #title>
            <strong>{{ validationResult.is_valid ? '✅ 数据正常' : '⚠️ 数据不完整' }}</strong>
            <span style="margin-left: 10px; font-weight: normal;">{{ validationResult.message }}</span>
          </template>
        </el-alert>

        <!-- 缺失数据提示 -->
        <el-alert
          v-if="validationResult.missing_data && validationResult.missing_data.length > 0"
          type="error"
          :closable="false"
          show-icon
          style="margin-bottom: 20px"
        >
          <template #title>
            <strong>缺失数据：</strong>{{ validationResult.missing_data.join('、') }}
          </template>
        </el-alert>

        <!-- 详细数据表格 -->
        <div v-if="validationResult.details" class="validation-details">
          <div
            v-for="(table, index) in formatValidationDetails(validationResult.details)"
            :key="index"
            class="detail-table-section"
            style="margin-bottom: 24px"
          >
            <h4 style="margin-bottom: 12px; color: #409eff; font-size: 16px; font-weight: 600">
              {{ table.title }}
            </h4>
            <el-table
              :data="table.data"
              border
              stripe
              style="width: 100%"
              :show-header="false"
            >
              <el-table-column prop="label" width="180" label="项目">
                <template #default="{ row }">
                  <strong>{{ row.label }}</strong>
                </template>
              </el-table-column>
              <el-table-column prop="value" label="值">
                <template #default="{ row }">
                  <span v-html="row.value"></span>
                </template>
              </el-table-column>
            </el-table>
          </div>
        </div>
      </div>

      <template #footer>
        <div style="display: flex; align-items: center; gap: 12px; justify-content: space-between;">
          <div v-if="syncStatus !== 'idle'" style="display: flex; align-items: center; gap: 8px;">
            <el-icon v-if="syncStatus === 'running'" class="is-loading" style="animation: rotate 1.5s linear infinite;">
              <Loading />
            </el-icon>
            <el-icon v-else-if="syncStatus === 'success'" style="color: #67c23a;">
              <Check />
            </el-icon>
            <el-icon v-else style="color: #f56c6c;">
              <WarningFilled />
            </el-icon>
            <span>{{ syncStatus === 'running' ? '正在同步...' : syncStatus === 'success' ? '同步完成' : '同步失败' }}</span>
          </div>
          <div style="display: flex; gap: 12px;">
            <el-button type="primary" :disabled="syncStatus === 'running'" @click="handleSyncMissingData">
              <el-icon><Refresh /></el-icon>
              同步缺失数据
            </el-button>
            <el-button @click="showValidationDialog = false">关闭</el-button>
          </div>
        </div>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { useSingleAnalysis } from './useSingleAnalysis'

const {
  submitting,
  validatingData,
  showValidationDialog,
  validationResult,
  syncStatus,
  analysisStatus,
  showResults,
  analysisResults,
  activeReportTab,
  growthTaskId,
  progressInfo,
  showAdvanced,
  modelsReady,
  modelLoadError,
  formatResearchDate,
  searchStockSuggestions,
  selectStockSuggestion,
  modelSettings,
  availableModels,
  workflowsSorted,
  selectedWorkflowId,
  modelRecommendation,
  analysisForm,
  stockCodeError,
  stockCodeHelp,
  depthOptions,
  disabledDate,
  onStockCodeInput,
  availableAnalysts,
  onMarketChange,
  toggleAnalyst,
  handleValidateData,
  handleSyncMissingData,
  formatValidationDetails,
  submitAnalysis,
  restartAnalysis,
  startNewAnalysis,
  renderMarkdown,
  overviewBriefContent,
  getAnalysisReports,
  getReportIcon,
  getReportName,
  getReportDescription,
  formatReportContent,
  downloadReport,
  getDepthDescription,
  getProgressStatus,
  formatTime,
  initializeModelSettings,
  getCapabilityText,
  getCapabilityTagType,
  isQuickAnalysisRole,
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
} = useSingleAnalysis()
</script>

<style lang="scss" scoped src="./SingleAnalysis.scss"></style>
<style lang="scss" src="./SingleAnalysis.global.scss"></style>
