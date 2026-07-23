import { useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Drawer,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Select,
  Space,
  Spin,
  Tag,
  Upload,
  message,
} from 'antd';

import {
  analyzeWebsiteAdsCreativeAsset,
  analyzeWebsiteAdsProduct,
  executeWebsiteAdsMediaPlan,
  generateWebsiteAdsMediaPlan,
  getWebsiteAdsMediaPlanExecution,
  getWebsiteAdsMediaPlanGeneration,
  listWebsiteAdsContentProducts,
  syncWebsiteAdsCreativeAssets,
  updateWebsiteAdsCreativeAsset,
  updateWebsiteAdsProduct,
  uploadWebsiteAdsVideoFile,
  waitForWebsiteAdsVideoUploads,
} from './api.js';


const ROLE_LABELS = {
  CONTROL: '控制组',
  AUDIENCE_TEST: '受众测试组',
  CREATIVE_TEST: '创意测试组',
};

function money(value) {
  return `$${Number(value || 0).toFixed(2)}`;
}

function errorText(error, fallback) {
  const detail = error?.response?.data?.detail;
  return typeof detail === 'string' ? detail : error?.message || fallback;
}

function analysisLabel(status) {
  const labels = {
    READY: ['已分析', 'success'],
    ANALYZING: ['分析中', 'processing'],
    EXTRACTING: ['提取字幕与画面', 'processing'],
    QUEUED: ['等待分析', 'processing'],
    PARTIAL: ['资料受限', 'warning'],
    FAILED: ['分析失败', 'error'],
    NOT_ANALYZED: ['待分析', 'default'],
  };
  return labels[status] || ['待分析', 'default'];
}

const CREATIVE_TYPE_LABELS = {
  ugc_testimonial: 'UGC 口碑',
  testimonial: '口碑证言',
  product_demo: '产品演示',
  demonstration: '产品演示',
  problem_solution: '痛点解决',
  educational: '知识讲解',
  comparison: '对比测评',
  unboxing: '开箱',
  lifestyle: '生活方式',
  product_showcase: '商品展示',
};

const TALENT_TYPE_LABELS = {
  creator: '达人素材',
  influencer: '达人素材',
  customer: '用户出镜',
  founder: '品牌主理人',
  brand: '品牌素材',
  product_only: '纯商品',
  no_talent: '无人物',
};

function classificationLabel(value, labels, fallback) {
  const key = String(value || '').trim().toLowerCase();
  return labels[key] || (key && key !== 'unknown' && key !== 'unclassified' ? value : fallback);
}

export default function HermesManagedLaunch({
  open,
  onClose,
  workspaceId,
  provider,
  authId,
  products,
  initialProduct,
  onCompleted,
  onProductUpdated,
}) {
  const [form] = Form.useForm();
  const [productForm] = Form.useForm();
  const [assetForm] = Form.useForm();
  const [uploadForm] = Form.useForm();
  const [assets, setAssets] = useState([]);
  const [contentProducts, setContentProducts] = useState([]);
  const [plan, setPlan] = useState(null);
  const [planError, setPlanError] = useState('');
  const [planStatus, setPlanStatus] = useState('');
  const [loadingAssets, setLoadingAssets] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [previewAsset, setPreviewAsset] = useState(null);
  const [editingProduct, setEditingProduct] = useState(null);
  const [editingAsset, setEditingAsset] = useState(null);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [uploadFiles, setUploadFiles] = useState([]);
  const [uploadProgress, setUploadProgress] = useState(null);
  const generationAttemptRef = useRef(0);
  const selectedProductId = Form.useWatch('landing_page_id', form);
  const selectedProduct = useMemo(
    () => products.find((item) => Number(item.id) === Number(selectedProductId)),
    [products, selectedProductId],
  );
  const orderedAssets = useMemo(() => [...assets].sort((left, right) => {
    const leftMatch = Number(left?.product_match?.landing_page_id) === Number(selectedProductId)
      || Number(left?.landing_page_id) === Number(selectedProductId);
    const rightMatch = Number(right?.product_match?.landing_page_id) === Number(selectedProductId)
      || Number(right?.landing_page_id) === Number(selectedProductId);
    if (leftMatch !== rightMatch) return leftMatch ? -1 : 1;
    const originPriority = { REAL_CREATOR: 6, REAL_CUSTOMER: 5, BRAND_STAFF: 4, MIXED: 3, UNKNOWN: 2, AIGC: 1 };
    const originDifference = (originPriority[right.production_origin] || 0) - (originPriority[left.production_origin] || 0);
    if (originDifference) return originDifference;
    const readiness = { READY: 5, PARTIAL: 4, ANALYZING: 3, EXTRACTING: 2, QUEUED: 1 };
    return (readiness[right.analysis_status] || 0) - (readiness[left.analysis_status] || 0);
  }), [assets, selectedProductId]);
  const eligibleAssetCount = useMemo(() => orderedAssets.filter((asset) => {
    const matched = Number(asset?.product_match?.landing_page_id) === Number(selectedProductId)
      || Number(asset?.landing_page_id) === Number(selectedProductId);
    return matched && asset.analysis_status === 'READY';
  }).length, [orderedAssets, selectedProductId]);

  async function refreshAssets() {
    if (!authId) return;
    setLoadingAssets(true);
    try {
      const payload = await syncWebsiteAdsCreativeAssets(workspaceId, provider, authId);
      setAssets(payload.items || []);
    } catch (error) {
      message.error(errorText(error, '素材库同步失败'));
    } finally {
      setLoadingAssets(false);
    }
  }

  async function loadContentProducts() {
    if (!authId) return;
    try {
      const payload = await listWebsiteAdsContentProducts(workspaceId, provider, authId);
      setContentProducts(payload.items || []);
    } catch (error) {
      message.error(errorText(error, '内容工厂商品读取失败'));
    }
  }

  useEffect(() => {
    if (!open) {
      generationAttemptRef.current += 1;
      setSubmitting(false);
      return undefined;
    }
    const productId = initialProduct?.id || products[0]?.id;
    form.setFieldsValue({
      landing_page_id: productId,
      daily_budget: 60,
      request_notes: '',
    });
    setPlan(null);
    setPlanError('');
    setPlanStatus('');
    setSubmitting(false);
    refreshAssets();
    loadContentProducts();
    return () => {
      generationAttemptRef.current += 1;
    };
  }, [open, authId, initialProduct?.id]);

  async function generatePlan() {
    const values = await form.validateFields();
    const generationAttempt = generationAttemptRef.current + 1;
    generationAttemptRef.current = generationAttempt;
    setPlanError('');
    setSubmitting(true);
    try {
      const job = await generateWebsiteAdsMediaPlan(workspaceId, provider, authId, {
        landing_page_id: values.landing_page_id,
        daily_budget: values.daily_budget,
        request_notes: values.request_notes || undefined,
        activate_after_create: true,
      });
      setPlanStatus('任务已提交，正在等待 Hermes 规划 worker 接管…');
      const deadline = Date.now() + (25 * 60 * 1000);
      let result = null;
      let pollCount = 0;
      while (Date.now() < deadline) {
        if (generationAttemptRef.current !== generationAttempt) return;
        // Each status request is short; the long-running model call stays in the worker.
        // eslint-disable-next-line no-await-in-loop
        const state = await getWebsiteAdsMediaPlanGeneration(workspaceId, provider, authId, job.plan_id);
        if (state.message) setPlanStatus(state.message);
        if (state.state === 'READY' && state.plan) {
          result = state.plan;
          break;
        }
        if (state.state === 'FAILED') {
          throw new Error(state.error || 'Hermes 投放方案生成失败');
        }
        pollCount += 1;
        // eslint-disable-next-line no-await-in-loop
        await new Promise((resolve) => setTimeout(resolve, Math.min(5000, 1500 + (pollCount * 250))));
      }
      if (generationAttemptRef.current !== generationAttempt) return;
      if (!result) throw new Error('Hermes 投放方案生成超时，请稍后查看方案列表');
      setPlan(result);
      setPlanStatus('');
      message.success('Hermes 已生成受约束的投放方案');
    } catch (error) {
      const description = errorText(error, 'Hermes 投放方案生成失败');
      setPlanStatus('');
      setPlanError(description);
      message.error(description);
    } finally {
      if (generationAttemptRef.current === generationAttempt) setSubmitting(false);
    }
  }

  async function executePlan() {
    if (!plan?.id) return;
    const executionAttempt = generationAttemptRef.current + 1;
    generationAttemptRef.current = executionAttempt;
    setPlanError('');
    setPlanStatus('已提交创建任务，正在预检素材、封面和 TikTok 参数…');
    setSubmitting(true);
    try {
      const job = await executeWebsiteAdsMediaPlan(workspaceId, provider, authId, plan.id);
      const deadline = Date.now() + (25 * 60 * 1000);
      let result = job.result || null;
      let pollCount = 0;
      while (!result && Date.now() < deadline) {
        if (generationAttemptRef.current !== executionAttempt) return;
        // The server owns the long-running TikTok write transaction; this request only observes it.
        // eslint-disable-next-line no-await-in-loop
        const state = await getWebsiteAdsMediaPlanExecution(workspaceId, provider, authId, plan.id);
        if (['CREATED', 'ACTIVE'].includes(state.state) && state.result) {
          result = state.result;
          break;
        }
        if (state.state === 'FAILED') {
          throw new Error(state.error || 'TikTok 投放创建失败，系统已清理未完成的系列');
        }
        setPlanStatus(state.state === 'EXECUTING'
          ? 'TikTok 正在创建广告系列、广告组和广告，所有对象会在完整成功后统一开启…'
          : '创建任务正在排队，稍后将自动执行…');
        pollCount += 1;
        // eslint-disable-next-line no-await-in-loop
        await new Promise((resolve) => setTimeout(resolve, Math.min(5000, 1500 + (pollCount * 250))));
      }
      if (generationAttemptRef.current !== executionAttempt) return;
      if (!result) throw new Error('TikTok 投放创建超时，请稍后在系列列表查看最终状态');
      setPlanStatus('');
      message.success(`已创建 ${result.adgroup_count || 0} 个广告组和 ${result.ad_count || 0} 条广告`);
      onCompleted?.(result);
      onClose();
    } catch (error) {
      const description = errorText(error, '媒体方案执行失败，未完成的系列已自动清理');
      setPlanStatus('');
      setPlanError(description);
      message.error(description);
    } finally {
      if (generationAttemptRef.current === executionAttempt) setSubmitting(false);
    }
  }

  function openProductEditor() {
    if (!selectedProduct) return;
    setEditingProduct(selectedProduct);
    productForm.setFieldsValue({
      title: selectedProduct.title,
      content_product_id: selectedProduct.content_product_id,
      landing_url: selectedProduct.landing_url,
      reference_price: selectedProduct.reference_price,
      currency: selectedProduct.currency || 'USD',
      image_url: selectedProduct.image_url,
      seller_profile: selectedProduct.seller_profile,
      promotion_text: selectedProduct.promotion_text,
      product_details: selectedProduct.product_details,
    });
  }

  async function saveProduct() {
    const values = await productForm.validateFields();
    setSubmitting(true);
    try {
      const updated = await updateWebsiteAdsProduct(workspaceId, provider, authId, editingProduct.id, values);
      const analyzed = await analyzeWebsiteAdsProduct(workspaceId, provider, authId, updated.id);
      onProductUpdated?.(analyzed);
      setEditingProduct(null);
      message.success('商品资料已保存，Hermes 分析已更新');
    } catch (error) {
      message.error(errorText(error, '商品资料保存失败'));
    } finally {
      setSubmitting(false);
    }
  }

  function openAssetEditor(asset) {
    setEditingAsset(asset);
    assetForm.setFieldsValue({
      title: asset.title,
      landing_page_id: asset.landing_page_id || selectedProductId,
      user_notes: asset.user_notes,
      tags: asset.tags || [],
    });
  }

  async function saveAsset() {
    const values = await assetForm.validateFields();
    setSubmitting(true);
    try {
      const updated = await updateWebsiteAdsCreativeAsset(workspaceId, provider, authId, editingAsset.id, values);
      const analyzed = await analyzeWebsiteAdsCreativeAsset(workspaceId, provider, authId, updated.id);
      setAssets((current) => current.map((item) => (item.id === analyzed.id ? analyzed : item)));
      setEditingAsset(null);
      message.success('素材档案已保存，已提交字幕、画面与分类分析');
    } catch (error) {
      message.error(errorText(error, '素材档案保存失败'));
    } finally {
      setSubmitting(false);
    }
  }

  async function uploadVideo() {
    const pendingFiles = uploadFiles.filter((item) => item.originFileObj);
    if (!pendingFiles.length) {
      message.warning('请选择一个或多个本地视频文件');
      return;
    }
    setSubmitting(true);
    setUploadProgress({ completed: 0, total: pendingFiles.length, failed: 0, duplicates: 0 });
    const failures = [];
    let completed = 0;
    let duplicates = 0;
    const backgroundUploadIds = [];
    try {
      for (const item of pendingFiles) {
        const sourceFile = item.originFileObj;
        try {
          const formData = new FormData();
          formData.append('video_file', sourceFile);
          formData.append('file_name', sourceFile.name.slice(0, 100));
          formData.append('flaw_detect', 'false');
          formData.append('auto_fix_enabled', 'false');
          // Sequential uploads keep TikTok API pressure predictable while still supporting one drag-and-drop batch.
          // eslint-disable-next-line no-await-in-loop
          const response = await uploadWebsiteAdsVideoFile(workspaceId, provider, authId, formData);
          if (response?.in_progress && response?.upload_id) backgroundUploadIds.push(Number(response.upload_id));
          else if (response?.deduplicated || response?.skipped) duplicates += 1;
          else completed += 1;
        } catch (error) {
          failures.push({ item, error });
        }
        setUploadProgress({ completed: completed + duplicates + failures.length + backgroundUploadIds.length, total: pendingFiles.length, failed: failures.length, duplicates });
      }
      if (completed || duplicates) await refreshAssets();
      if (backgroundUploadIds.length) {
        const notificationKey = `website-upload-${backgroundUploadIds.join('-')}`;
        message.open({ key: notificationKey, type: 'loading', duration: 0, content: `已落盘 ${backgroundUploadIds.length} 个视频，正在后台上传 TikTok` });
        void waitForWebsiteAdsVideoUploads(workspaceId, provider, authId, backgroundUploadIds, {
          onProgress: ({ completed: done, failed, total }) => {
            message.open({ key: notificationKey, type: 'loading', duration: 0, content: `后台上传 ${done + failed}/${total}，可继续配置投放` });
          },
        }).then(async ({ completed: done, failed, total }) => {
          await refreshAssets();
          message.open({
            key: notificationKey,
            type: failed ? 'warning' : 'success',
            duration: 6,
            content: failed ? `后台上传完成 ${done}/${total}，失败 ${failed} 个，可在素材库重试` : `后台上传完成，共新增 ${done} 个视频`,
          });
        }).catch((error) => {
          message.open({ key: notificationKey, type: 'info', duration: 6, content: errorText(error, '后台仍在继续上传，可稍后刷新素材库查看') });
        });
      }
      if (failures.length) {
        setUploadFiles(failures.map(({ item }) => item));
        message.warning(`新增 ${completed} 个，自动忽略重复 ${duplicates} 个，失败 ${failures.length} 个；失败文件已保留`);
      } else {
        setUploadOpen(false);
        uploadForm.resetFields();
        setUploadFiles([]);
        if (backgroundUploadIds.length) message.success(`已提交 ${backgroundUploadIds.length} 个后台上传任务，页面可正常继续使用`);
        else if (completed) message.success(`新增 ${completed} 个视频，自动忽略重复 ${duplicates} 个；Hermes 正在自动分析新素材`);
        else message.info(`所选 ${duplicates} 个视频均已存在，系统已自动忽略，未重复上传`);
      }
    } catch (error) {
      message.error(errorText(error, '视频上传或素材同步失败'));
    } finally {
      setSubmitting(false);
      if (!failures.length) setUploadProgress(null);
    }
  }

  const productReady = selectedProduct?.reference_price && selectedProduct?.seller_profile && selectedProduct?.effective_product_details;

  return <>
    <Drawer
      title="Hermes 托管投放"
      open={open}
      onClose={onClose}
      width={900}
      extra={plan
        ? <Button type="primary" loading={submitting} onClick={executePlan}>确认创建并开始投放</Button>
        : <Button type="primary" loading={submitting} disabled={!eligibleAssetCount} onClick={generatePlan}>生成投放方案</Button>}
    >
      <div className="website-ads-hermes-intro">
        <strong>用户只负责商品事实和预算，Hermes 负责素材理解、精准定向和实验设计</strong>
        <span>系统自动识别商品与视频内容，优先选择真实达人和真实用户素材，并用多个独立受众组验证点击质量。</span>
      </div>
      <Form form={form} layout="vertical" onValuesChange={() => { setPlan(null); setPlanError(''); setPlanStatus(''); }}>
        <div className="website-ads-form-section">
          <div className="website-ads-form-section-head">
            <div><h3>1. 选择商品</h3><span className="website-ads-section-note">商品资料必须包含真实售价、卖家信息和促销活动</span></div>
            <Button onClick={openProductEditor} disabled={!selectedProduct}>完善商品资料</Button>
          </div>
          <Form.Item name="landing_page_id" label="推广商品" rules={[{ required: true, message: '请选择商品' }]}>
            <Select options={products.map((item) => ({ value: item.id, label: item.title }))} />
          </Form.Item>
          {selectedProduct && <div className="website-ads-hermes-product">
            {selectedProduct.image_url ? <img src={selectedProduct.image_url} alt="" /> : <div className="website-ads-mini-placeholder">无图</div>}
            <div><strong>{selectedProduct.title}</strong><span>{selectedProduct.landing_url}</span><span>跳转商品：{selectedProduct.tiktok_shop_url || `TikTok Shop 商品 ${selectedProduct.product_id || '待绑定'}`}</span><span>真实参考成交价 {selectedProduct.currency || 'USD'} {Number(selectedProduct.reference_price || 0).toFixed(2)}</span></div>
            {selectedProduct.content_product ? <Tag color="purple">继承内容工厂</Tag> : null}
            <Tag color={analysisLabel(selectedProduct.analysis_status)[1]}>{analysisLabel(selectedProduct.analysis_status)[0]}</Tag>
          </div>}
          {!productReady && <Alert type="warning" showIcon message="商品事实尚不完整" description="请补充真实卖家、产品资料和参考成交价。促销活动没有时应明确填写“无促销”，避免 Hermes 推测。" />}
        </div>

        <div className="website-ads-form-section">
          <div className="website-ads-form-section-head">
            <div><h3>2. Hermes 自动素材池</h3><span className="website-ads-section-note">无需人工勾选；按商品匹配、内容形式、历史效果和探索价值自动筛选</span></div>
            <Space><Button onClick={refreshAssets} loading={loadingAssets}>同步素材库</Button><Button type="primary" ghost onClick={() => setUploadOpen(true)}>上传素材</Button></Space>
          </div>
          <div className="website-ads-hermes-assets">
              {loadingAssets ? <Spin /> : orderedAssets.length ? orderedAssets.map((asset) => {
                const status = analysisLabel(asset.analysis_status);
                const matched = Number(asset?.product_match?.landing_page_id) === Number(selectedProductId)
                  || Number(asset?.landing_page_id) === Number(selectedProductId);
                return <article key={asset.id} className={matched ? 'matched' : ''}>
                  <button type="button" className="website-ads-asset-preview" onClick={() => setPreviewAsset(asset)}>
                    {asset.cover_url ? <img src={asset.cover_url} alt="" /> : <span>视频</span>}
                  </button>
                  <div className="website-ads-asset-body">
                    <strong title={asset.title}>{asset.title}</strong>
                    <span>{asset.duration_seconds ? `${Number(asset.duration_seconds).toFixed(1)} 秒` : '时长待同步'} · {asset.width && asset.height ? `${asset.width}×${asset.height}` : '尺寸待同步'}</span>
                    <div className="website-ads-asset-tags">
                      <Tag color={status[1]}>{status[0]}</Tag>
                      <Tag color={asset.source === 'SPARK_AUTHORIZED_POST' ? 'cyan' : 'default'}>{asset.source_label || '广告主素材库'}</Tag>
                      {matched ? <Tag color="blue">当前商品</Tag> : null}
                      <Tag>{classificationLabel(asset.creative_type, CREATIVE_TYPE_LABELS, '形式待识别')}</Tag>
                      <Tag>{classificationLabel(asset.talent_type, TALENT_TYPE_LABELS, '人物待识别')}</Tag>
                      {asset.production_origin === 'REAL_CREATOR' ? <Tag color="green">真实达人优先</Tag> : null}
                      {asset.production_origin === 'AIGC' ? <Tag color="orange">AIGC 备选</Tag> : null}
                    </div>
                    <div><span>{asset.transcript_excerpt ? '字幕已提取' : '字幕待提取'}</span><Button type="link" size="small" onClick={() => openAssetEditor(asset)}>纠正资料</Button></div>
                  </div>
                </article>;
              }) : <Empty description="素材库为空，请先同步或上传视频" />}
          </div>
          {!loadingAssets && !eligibleAssetCount ? <Alert type="info" showIcon message="正在建立当前商品的可投素材池" description="Hermes 会在字幕、关键画面和商品匹配分析完成后自动开放投放方案，无需人工选择。" /> : null}
        </div>

        <div className="website-ads-form-section">
          <h3>3. 输入总预算</h3>
          <div className="website-ads-form-grid">
            <Form.Item name="daily_budget" label="每日总预算" extra="至少建立 3 个精准定向组，每组不少于 20 美元并安排 4–6 条视频；最高可建立 6 个组。" rules={[{ required: true }]}>
              <InputNumber min={60} precision={2} addonBefore="$" className="website-ads-full" />
            </Form.Item>
            <Form.Item name="request_notes" label="运营补充（可选）">
              <Input placeholder="例如：本周主推买二优惠，不使用医疗功效表述" />
            </Form.Item>
          </div>
        </div>
      </Form>

      {planStatus ? <Alert type="info" showIcon message={plan ? 'TikTok 正在创建投放' : 'Hermes 正在生成投放方案'} description={planStatus} /> : null}
      {planError ? <Alert type="error" showIcon closable onClose={() => setPlanError('')} message={plan ? '投放创建失败' : '投放方案生成失败'} description={planError} /> : null}

      {plan && <div className="website-ads-plan-review">
        <div className="website-ads-form-section-head"><div><h3>Hermes 投放方案</h3><span>{plan.strategy_summary}</span></div><Space><Tag color="cyan">业务目标：CTR ≥ 4% · CPC ≤ $0.30</Tag><Tag color={plan.strategy_source === 'HERMES' ? 'blue' : 'orange'}>{plan.strategy_source === 'HERMES' ? 'Hermes 决策' : '安全基线方案'}</Tag><Tag>{plan.confidence || '待评估'}</Tag></Space></div>
        <div className="website-ads-plan-budget"><span>每日总预算</span><strong>{money(plan.daily_budget)}</strong><span>{plan.groups?.length || 0} 个广告组</span></div>
        <div className="website-ads-plan-groups">
          {(plan.groups || []).map((group) => <article key={group.id}>
            <div><Tag color={group.role === 'CONTROL' ? 'default' : 'blue'}>{ROLE_LABELS[group.role] || group.role}</Tag><strong>{group.name}</strong><b>{money(group.daily_budget)}/日</b></div>
            <p>{group.hypothesis}</p>
            <span>受众：{group.targeting?.audience_segment || group.name} · {group.targeting?.interest_category_ids?.length || 0} 个已验证兴趣 · {(group.targeting?.age_groups || []).join('、')}</span>
            <div className="website-ads-plan-creatives">{(group.creatives || []).map((creative) => <span key={creative.id}>{creative.cover_url ? <img src={creative.cover_url} alt="" /> : null}<em>{creative.title}</em></span>)}</div>
          </article>)}
        </div>
        <Alert type="info" showIcon message="确认后由 Hermes 创建并开启" description="创建过程中任何一层失败，系统都会保持系列暂停并写入操作审计；不会留下继续消耗的半成品。" />
      </div>}
    </Drawer>

    <Modal title={previewAsset?.title || '素材预览'} open={Boolean(previewAsset)} onCancel={() => setPreviewAsset(null)} footer={null} width={480}>
      {previewAsset?.preview_url ? <video className="website-ads-preview-player" src={previewAsset.preview_url} poster={previewAsset.cover_url} controls autoPlay /> : <Empty description="TikTok 暂未返回预览地址" />}
      {previewAsset?.source === 'SPARK_AUTHORIZED_POST' ? <Alert
        type="info"
        showIcon
        message={`达人授权素材 · ${previewAsset.creator_name || 'TikTok 账号'}`}
        description={`授权状态：${previewAsset.authorization_status || '待核验'}${previewAsset.authorization_end_time ? ` · 有效期至 ${previewAsset.authorization_end_time} UTC` : ''}`}
      /> : null}
      {previewAsset?.contact_sheet_url ? <img className="website-ads-contact-sheet" src={previewAsset.contact_sheet_url} alt="视频关键画面" /> : null}
      {previewAsset?.transcript_excerpt ? <div className="website-ads-transcript"><strong>识别字幕</strong><p>{previewAsset.transcript_excerpt}</p></div> : null}
    </Modal>

    <Modal title="商品资料与 Hermes 分析" open={Boolean(editingProduct)} onCancel={() => setEditingProduct(null)} onOk={saveProduct} confirmLoading={submitting} okText="保存并重新分析" width={680}>
      <Form form={productForm} layout="vertical">
        <Form.Item name="content_product_id" label="继承内容工厂商品" extra="内容工厂提供经过核验的商品事实、允许表述与禁用表述；Magento 仍负责落地页、售价和 TikTok Shop 商品映射。">
          <Select
            allowClear
            showSearch
            optionFilterProp="label"
            placeholder="选择内容工厂中的同一商品"
            options={contentProducts.map((item) => ({
              value: item.id,
              label: `${item.brand_name} · ${item.product_name} (${item.market})`,
            }))}
          />
        </Form.Item>
        <Form.Item name="title" label="商品名称" rules={[{ required: true }]}><Input /></Form.Item>
        <Form.Item name="landing_url" label="落地页" rules={[{ required: true, type: 'url' }]}><Input /></Form.Item>
        <Form.Item label="TikTok Shop 跳转商品"><Input value={editingProduct?.tiktok_shop_url || editingProduct?.product_id || ''} readOnly /></Form.Item>
        <Form.Item label="广告追踪链接预览" extra="创建广告时自动使用 TikTok 宏参数；ttclid 由 TikTok 自动附加，Magento 会记录并继续传递。"><Input.TextArea value={editingProduct?.tracking_url_preview || ''} autoSize={{ minRows: 2, maxRows: 4 }} readOnly /></Form.Item>
        <div className="website-ads-form-grid"><Form.Item name="reference_price" label="真实参考成交价" rules={[{ required: true }]}><InputNumber min={0.01} precision={2} addonBefore="$" className="website-ads-full" /></Form.Item><Form.Item name="currency" label="币种"><Input maxLength={8} /></Form.Item></div>
        <Form.Item name="seller_profile" label="真实卖家与品牌信息" rules={[{ required: true, message: '请填写真实卖家和品牌信息' }]}><Input.TextArea rows={3} placeholder="品牌、发货地、售后承诺、可验证资质等" /></Form.Item>
        <Form.Item name="promotion_text" label="当前促销活动"><Input.TextArea rows={3} placeholder="没有促销时请明确填写“无促销”" /></Form.Item>
        {editingProduct?.content_product ? <Alert type="success" showIcon message={`已继承：${editingProduct.content_product.product_name}`} description="内容工厂的核验事实会实时用于 Hermes 决策。这里只填写落地页特有、且内容工厂尚未包含的补充信息。" /> : null}
        <Form.Item name="product_details" label="落地页商品补充（可选）"><Input.TextArea rows={5} placeholder="可补充本落地页特有的规格、卖点或限制；留空时使用内容工厂核验事实" /></Form.Item>
        <Form.Item name="image_url" label="商品主图 URL"><Input /></Form.Item>
      </Form>
    </Modal>

    <Modal title="素材档案" open={Boolean(editingAsset)} onCancel={() => setEditingAsset(null)} onOk={saveAsset} confirmLoading={submitting} okText="保存并分析" width={620}>
      <Form form={assetForm} layout="vertical">
        <Form.Item name="title" label="素材标题" rules={[{ required: true }]}><Input /></Form.Item>
        <Form.Item name="landing_page_id" label="关联商品"><Select allowClear options={products.map((item) => ({ value: item.id, label: item.title }))} /></Form.Item>
        <Form.Item name="tags" label="标签"><Select mode="tags" tokenSeparators={[',']} placeholder="UGC、开箱、痛点、演示等" /></Form.Item>
        <Form.Item name="user_notes" label="真实素材说明"><Input.TextArea rows={5} placeholder="出镜人、核心话术、促销内容、历史表现或需要避免的表述" /></Form.Item>
      </Form>
    </Modal>

    <Modal title="批量上传到 TikTok 素材库" open={uploadOpen} onCancel={() => { setUploadOpen(false); setUploadFiles([]); setUploadProgress(null); }} onOk={uploadVideo} confirmLoading={submitting} okText={`上传 ${uploadFiles.length || ''}`}>
      <Form form={uploadForm} layout="vertical">
        <Alert type="info" showIcon message="无需手动填写素材说明" description="上传后系统会提取字幕和关键画面，由 Hermes 自动生成视频描述、识别商品与素材来源，并优先标记真实达人内容。" />
        <Form.Item label="视频文件" required>
          <Upload.Dragger accept=".mp4,.mov,.mpeg,.avi" multiple maxCount={20} fileList={uploadFiles} beforeUpload={() => false} onChange={({ fileList }) => setUploadFiles(fileList.slice(-20))}>
            <p>点击选择或拖入多个视频</p><span>单次最多 20 个；支持 MP4、MOV、MPEG、AVI，单文件最大 500 MB</span>
          </Upload.Dragger>
        </Form.Item>
        {uploadProgress ? <Alert type={uploadProgress.failed ? 'warning' : 'info'} showIcon message={`处理进度 ${uploadProgress.completed}/${uploadProgress.total}`} description={`新增与重复文件会自动区分${uploadProgress.duplicates ? `，已忽略重复 ${uploadProgress.duplicates} 个` : ''}${uploadProgress.failed ? `，失败 ${uploadProgress.failed} 个` : ''}。`} /> : null}
      </Form>
    </Modal>
  </>;
}
