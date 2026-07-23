import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Descriptions,
  Empty,
  Form,
  Input,
  Modal,
  Pagination,
  Segmented,
  Select,
  Space,
  Spin,
  Statistic,
  Tag,
  Upload,
  message,
} from 'antd';

import {
  analyzeWebsiteAdsCreativeAsset,
  listWebsiteAdsCreativeAssets,
  syncWebsiteAdsCreativeAssets,
  updateWebsiteAdsCreativeAsset,
  uploadWebsiteAdsVideoByUrl,
  uploadWebsiteAdsVideoFile,
  waitForWebsiteAdsVideoUploads,
} from './api.js';


const PAGE_SIZE = 12;
const PROCESSING_STATUSES = new Set(['QUEUED', 'EXTRACTING', 'ANALYZING']);
const AUTO_PROCESSING_STATUSES = new Set(['DEPLOYING']);

const ANALYSIS_META = {
  READY: ['已分析', 'success'],
  ANALYZING: ['Hermes 分析中', 'processing'],
  EXTRACTING: ['提取字幕与画面', 'processing'],
  QUEUED: ['等待分析', 'processing'],
  PARTIAL: ['资料受限', 'warning'],
  FAILED: ['分析失败', 'error'],
  NOT_ANALYZED: ['待分析', 'default'],
};

const AUTO_LAUNCH_META = {
  PENDING: ['待自动评估', 'default'],
  DEPLOYING: ['正在编排投放', 'processing'],
  DEPLOYED: ['已加入投放', 'success'],
  PARTIAL: ['部分已加入', 'warning'],
  RETRY: ['等待自动重试', 'warning'],
  WAITING_ANALYSIS: ['等待素材分析', 'default'],
  WAITING_PRODUCT: ['等待商品匹配', 'default'],
  WAITING_CAMPAIGN: ['等待托管计划', 'default'],
  WAITING_CAPACITY: ['等待广告位', 'warning'],
  WAITING_HERMES: ['Hermes 暂缓', 'warning'],
  BLOCKED: ['旧风险状态待重评', 'warning'],
  AUDIT_REJECTED: ['TikTok 审核拒绝', 'error'],
};

const POLICY_RISK_META = {
  APPROVED: ['低风险', 'success'],
  REVIEW: ['待平台核验', 'warning'],
  BLOCKED: ['高风险提示', 'error'],
};

const CREATIVE_TYPE_LABELS = {
  ugc: 'UGC',
  ugc_testimonial: 'UGC 口碑',
  creator_endorsement: '达人推荐',
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

function analysisMeta(status) {
  return ANALYSIS_META[String(status || '').toUpperCase()] || ['待分析', 'default'];
}

function autoLaunchMeta(asset) {
  const status = String(asset?.auto_launch_status || 'PENDING').toUpperCase();
  if (status === 'DEPLOYED' && asset?.auto_launch_decision?.delivery_enabled === false) {
    return ['已预置，随计划恢复', 'blue'];
  }
  return AUTO_LAUNCH_META[status] || ['待自动评估', 'default'];
}

function policyRiskMeta(asset) {
  const readiness = String(asset?.policy_readiness || 'REVIEW').toUpperCase();
  return POLICY_RISK_META[readiness] || POLICY_RISK_META.REVIEW;
}

function classificationLabel(value) {
  const key = String(value || '').trim().toLowerCase();
  return CREATIVE_TYPE_LABELS[key] || (key && key !== 'unknown' && key !== 'unclassified' ? value : '形式待识别');
}

function errorText(error, fallback) {
  const detail = error?.response?.data?.detail;
  return typeof detail === 'string' ? detail : error?.message || fallback;
}

function timeLabel(value) {
  if (!value) return '尚未完成';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value);
  return parsed.toLocaleString('zh-CN', { hour12: false });
}

function shortId(value) {
  const text = String(value || '');
  if (!text) return '待同步';
  return text.length > 16 ? `${text.slice(0, 8)}...${text.slice(-5)}` : text;
}

export default function CreativeAssetLibrary({ workspaceId, provider, authId, products = [] }) {
  const [assetForm] = Form.useForm();
  const [uploadForm] = Form.useForm();
  const [assets, setAssets] = useState([]);
  const [loading, setLoading] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [search, setSearch] = useState('');
  const [sourceFilter, setSourceFilter] = useState('all');
  const [statusFilter, setStatusFilter] = useState('all');
  const [productFilter, setProductFilter] = useState('all');
  const [page, setPage] = useState(1);
  const [previewAsset, setPreviewAsset] = useState(null);
  const [editingAsset, setEditingAsset] = useState(null);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [uploadMode, setUploadMode] = useState('file');
  const [uploadFiles, setUploadFiles] = useState([]);
  const [uploadProgress, setUploadProgress] = useState(null);

  const loadAssets = useCallback(async ({ quiet = false } = {}) => {
    if (!authId) return;
    if (!quiet) setLoading(true);
    try {
      const payload = await listWebsiteAdsCreativeAssets(workspaceId, provider, authId);
      setAssets(payload.items || []);
    } catch (error) {
      if (!quiet) message.error(errorText(error, '素材库读取失败'));
    } finally {
      if (!quiet) setLoading(false);
    }
  }, [workspaceId, provider, authId]);

  useEffect(() => {
    setPage(1);
    loadAssets();
  }, [loadAssets]);

  const hasProcessingAssets = useMemo(
    () => assets.some((asset) => (
      PROCESSING_STATUSES.has(String(asset.analysis_status || '').toUpperCase())
      || AUTO_PROCESSING_STATUSES.has(String(asset.auto_launch_status || '').toUpperCase())
    )),
    [assets],
  );

  useEffect(() => {
    if (!hasProcessingAssets) return undefined;
    const timer = window.setInterval(() => loadAssets({ quiet: true }), 15000);
    return () => window.clearInterval(timer);
  }, [hasProcessingAssets, loadAssets]);

  const productById = useMemo(
    () => new Map(products.map((product) => [Number(product.id), product])),
    [products],
  );

  const summary = useMemo(() => assets.reduce((total, asset) => {
    total.total += 1;
    if (asset.source === 'SPARK_AUTHORIZED_POST') total.spark += 1;
    else total.library += 1;
    if (asset.analysis_status === 'READY') total.ready += 1;
    if (PROCESSING_STATUSES.has(asset.analysis_status)) total.processing += 1;
    if (['FAILED', 'PARTIAL', 'NOT_ANALYZED'].includes(asset.analysis_status)) total.attention += 1;
    return total;
  }, { total: 0, spark: 0, library: 0, ready: 0, processing: 0, attention: 0 }), [assets]);

  const filteredAssets = useMemo(() => {
    const keyword = search.trim().toLowerCase();
    const filtered = assets.filter((asset) => {
      const matchedProductId = Number(asset.landing_page_id || asset.product_match?.landing_page_id || 0);
      const matchesSearch = !keyword || [
        asset.title,
        asset.file_name,
        asset.video_id,
        asset.creator_name,
        ...(asset.tags || []),
      ].some((value) => String(value || '').toLowerCase().includes(keyword));
      const matchesSource = sourceFilter === 'all'
        || (sourceFilter === 'spark' && asset.source === 'SPARK_AUTHORIZED_POST')
        || (sourceFilter === 'library' && asset.source !== 'SPARK_AUTHORIZED_POST');
      const matchesStatus = statusFilter === 'all'
        || (statusFilter === 'ready' && asset.analysis_status === 'READY')
        || (statusFilter === 'processing' && PROCESSING_STATUSES.has(asset.analysis_status))
        || (statusFilter === 'attention' && ['FAILED', 'PARTIAL', 'NOT_ANALYZED'].includes(asset.analysis_status));
      const matchesProduct = productFilter === 'all'
        || (productFilter === 'unassigned' && !matchedProductId)
        || Number(productFilter) === matchedProductId;
      return matchesSearch && matchesSource && matchesStatus && matchesProduct;
    });
    const originPriority = { REAL_CREATOR: 6, REAL_CUSTOMER: 5, BRAND_STAFF: 4, MIXED: 3, UNKNOWN: 2, AIGC: 1 };
    return filtered.sort((left, right) => {
      const originDifference = (originPriority[right.production_origin] || 0) - (originPriority[left.production_origin] || 0);
      if (originDifference) return originDifference;
      return new Date(right.last_synced_at || 0).getTime() - new Date(left.last_synced_at || 0).getTime();
    });
  }, [assets, productFilter, search, sourceFilter, statusFilter]);

  const visibleAssets = useMemo(
    () => filteredAssets.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE),
    [filteredAssets, page],
  );

  useEffect(() => {
    setPage(1);
  }, [search, sourceFilter, statusFilter, productFilter]);

  async function syncAssets() {
    setSyncing(true);
    try {
      const payload = await syncWebsiteAdsCreativeAssets(workspaceId, provider, authId);
      setAssets(payload.items || []);
      const dispatchCount = Number(payload.analysis_dispatch?.queued || 0);
      message.success(`已同步 ${payload.total || 0} 条素材${dispatchCount ? `，${dispatchCount} 条进入 Hermes 分析队列` : ''}`);
    } catch (error) {
      message.error(errorText(error, 'TikTok 素材同步失败'));
    } finally {
      setSyncing(false);
    }
  }

  function openEditor(asset) {
    setEditingAsset(asset);
    assetForm.setFieldsValue({
      title: asset.title,
      landing_page_id: asset.landing_page_id || asset.product_match?.landing_page_id,
      tags: asset.tags || [],
      user_notes: asset.user_notes,
    });
  }

  async function saveAsset() {
    const values = await assetForm.validateFields();
    setSaving(true);
    try {
      const updated = await updateWebsiteAdsCreativeAsset(workspaceId, provider, authId, editingAsset.id, values);
      const queued = await analyzeWebsiteAdsCreativeAsset(workspaceId, provider, authId, updated.id);
      setAssets((current) => current.map((item) => (item.id === queued.id ? queued : item)));
      setEditingAsset(null);
      message.success('素材档案已保存，并重新提交 Hermes 分析');
    } catch (error) {
      message.error(errorText(error, '素材档案保存失败'));
    } finally {
      setSaving(false);
    }
  }

  async function analyzeAsset(asset) {
    try {
      const queued = await analyzeWebsiteAdsCreativeAsset(workspaceId, provider, authId, asset.id);
      setAssets((current) => current.map((item) => (item.id === queued.id ? queued : item)));
      message.success('已提交 Hermes 重新分析');
    } catch (error) {
      message.error(errorText(error, '素材分析提交失败'));
    }
  }

  async function uploadAsset() {
    const values = uploadMode === 'url' ? await uploadForm.validateFields() : {};
    const pendingFiles = uploadFiles.filter((item) => item.originFileObj);
    if (uploadMode === 'file' && !pendingFiles.length) {
      message.warning('请选择一个或多个本地视频文件');
      return;
    }
    setSaving(true);
    setUploadProgress(uploadMode === 'file' ? { completed: 0, total: pendingFiles.length, failed: 0, duplicates: 0 } : null);
    const failures = [];
    let completed = 0;
    let duplicates = 0;
    const backgroundUploadIds = [];
    try {
      if (uploadMode === 'file') {
        for (const item of pendingFiles) {
          const sourceFile = item.originFileObj;
          try {
            const formData = new FormData();
            formData.append('video_file', sourceFile);
            formData.append('file_name', sourceFile.name.slice(0, 100));
            formData.append('flaw_detect', 'false');
            formData.append('auto_fix_enabled', 'false');
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
      } else {
        const response = await uploadWebsiteAdsVideoByUrl(workspaceId, provider, authId, {
          video_url: values.video_url,
          file_name: values.file_name,
        });
        if (response?.in_progress && response?.upload_id) backgroundUploadIds.push(Number(response.upload_id));
        else if (response?.deduplicated || response?.skipped) duplicates = 1;
        else completed = 1;
      }
      if (completed || duplicates) await syncAssets();
      if (backgroundUploadIds.length) {
        const notificationKey = `website-upload-${backgroundUploadIds.join('-')}`;
        message.open({ key: notificationKey, type: 'loading', duration: 0, content: `已落盘 ${backgroundUploadIds.length} 个视频，正在后台上传 TikTok` });
        void waitForWebsiteAdsVideoUploads(workspaceId, provider, authId, backgroundUploadIds, {
          onProgress: ({ completed: done, failed, total }) => {
            message.open({ key: notificationKey, type: 'loading', duration: 0, content: `后台上传 ${done + failed}/${total}，可继续使用其他功能` });
          },
        }).then(async ({ completed: done, failed, total }) => {
          await syncAssets();
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
        setUploadProgress(null);
        if (backgroundUploadIds.length) message.success(`已提交 ${backgroundUploadIds.length} 个后台上传任务，页面可正常继续使用`);
        else if (completed) message.success(`新增 ${completed} 个视频，自动忽略重复 ${duplicates} 个；Hermes 正在分析新素材`);
        else message.info(`所选 ${duplicates} 个视频均已存在，系统已自动忽略，未重复上传`);
      }
    } catch (error) {
      message.error(errorText(error, '视频上传或素材同步失败'));
    } finally {
      setSaving(false);
    }
  }

  return <section className="website-ads-panel website-ads-library-panel">
    <div className="website-ads-section-head">
      <div>
        <h2>素材管理</h2>
        <p>统一管理广告主素材、达人授权 Spark 视频、商品归属和 Hermes 内容分析。</p>
      </div>
      <Space wrap>
        <Button onClick={() => loadAssets()} loading={loading}>刷新本地数据</Button>
        <Button onClick={syncAssets} loading={syncing}>同步 TikTok</Button>
        <Button type="primary" onClick={() => setUploadOpen(true)}>上传视频</Button>
      </Space>
    </div>

    <div className="website-ads-library-summary" aria-label="素材概览">
      <Statistic title="全部素材" value={summary.total} />
      <Statistic title="达人授权 Spark" value={summary.spark} />
      <Statistic title="广告主素材库" value={summary.library} />
      <Statistic title="Hermes 已分析" value={summary.ready} />
      <Statistic title="分析处理中" value={summary.processing} />
      <Statistic title="需要处理" value={summary.attention} />
    </div>

    <div className="website-ads-library-toolbar">
      <Input.Search
        allowClear
        value={search}
        onChange={(event) => setSearch(event.target.value)}
        placeholder="搜索标题、达人、标签或素材 ID"
      />
      <Select value={sourceFilter} onChange={setSourceFilter} options={[
        { value: 'all', label: '全部来源' },
        { value: 'spark', label: '达人授权 Spark' },
        { value: 'library', label: '广告主素材库' },
      ]} />
      <Select value={statusFilter} onChange={setStatusFilter} options={[
        { value: 'all', label: '全部分析状态' },
        { value: 'ready', label: 'Hermes 已分析' },
        { value: 'processing', label: '分析处理中' },
        { value: 'attention', label: '需要处理' },
      ]} />
      <Select
        showSearch
        optionFilterProp="label"
        value={productFilter}
        onChange={setProductFilter}
        options={[
          { value: 'all', label: '全部商品' },
          { value: 'unassigned', label: '尚未关联商品' },
          ...products.map((product) => ({ value: String(product.id), label: product.title })),
        ]}
      />
      <span>显示 {filteredAssets.length} / {assets.length} 条</span>
    </div>

    {loading ? <div className="website-ads-library-loading"><Spin /></div> : visibleAssets.length ? <>
      <div className="website-ads-library-grid">
        {visibleAssets.map((asset) => {
          const status = analysisMeta(asset.analysis_status);
          const autoStatus = autoLaunchMeta(asset);
          const policyRisk = policyRiskMeta(asset);
          const matchedProductId = Number(asset.landing_page_id || asset.product_match?.landing_page_id || 0);
          const matchedProduct = productById.get(matchedProductId);
          const isSpark = asset.source === 'SPARK_AUTHORIZED_POST';
          return <article key={asset.id} className="website-ads-library-card">
            <button type="button" className="website-ads-library-cover" onClick={() => setPreviewAsset(asset)} aria-label={`预览 ${asset.title}`}>
              {asset.cover_url ? <img src={asset.cover_url} alt="" /> : <span>暂无封面</span>}
              <b>{asset.duration_seconds ? `${Number(asset.duration_seconds).toFixed(1)} 秒` : '视频'}</b>
            </button>
            <div className="website-ads-library-card-body">
              <div className="website-ads-library-card-tags">
                <Tag color={isSpark ? 'cyan' : 'default'}>{asset.source_label || '广告主素材库'}</Tag>
                <Tag color={status[1]}>{status[0]}</Tag>
                <Tag color={autoStatus[1]}>{autoStatus[0]}</Tag>
                <Tag color={policyRisk[1]} title={(asset.policy_flags || []).join('；')}>{policyRisk[0]}</Tag>
                {asset.production_origin === 'REAL_CREATOR' ? <Tag color="green">真实达人</Tag> : null}
                {asset.production_origin === 'REAL_CUSTOMER' ? <Tag color="cyan">真实用户</Tag> : null}
                {asset.production_origin === 'AIGC' ? <Tag color="orange">AIGC</Tag> : null}
              </div>
              <strong title={asset.title}>{asset.title || '未命名素材'}</strong>
              <span>{isSpark ? `达人：${asset.creator_name || '待同步'}` : `素材 ID：${shortId(asset.video_id)}`}</span>
              <span className={matchedProduct ? '' : 'is-muted'}>{matchedProduct ? `商品：${matchedProduct.title}` : '尚未关联商品'}</span>
              <div className="website-ads-library-classification">
                <Tag>{classificationLabel(asset.creative_type)}</Tag>
                {(asset.tags || []).slice(0, 2).map((tag) => <Tag key={tag}>{tag}</Tag>)}
              </div>
              {asset.analysis_error ? <span
                className={asset.analysis_status === 'FAILED' ? 'website-ads-library-error' : 'website-ads-library-warning'}
                title={asset.analysis_error}
              >{asset.analysis_status === 'FAILED' ? `分析失败：${asset.analysis_error}` : '模型结果受限，已采用保守分类'}</span> : null}
              {asset.auto_launch_error ? <span className="website-ads-library-warning" title={asset.auto_launch_error}>自动投放：{asset.auto_launch_error}</span> : null}
            </div>
            <div className="website-ads-library-card-actions">
              <Button size="small" onClick={() => setPreviewAsset(asset)}>预览</Button>
              <Button size="small" onClick={() => openEditor(asset)}>编辑</Button>
              <Button size="small" loading={PROCESSING_STATUSES.has(asset.analysis_status)} disabled={PROCESSING_STATUSES.has(asset.analysis_status)} onClick={() => analyzeAsset(asset)}>重新分析</Button>
            </div>
          </article>;
        })}
      </div>
      {filteredAssets.length > PAGE_SIZE ? <Pagination current={page} pageSize={PAGE_SIZE} total={filteredAssets.length} showSizeChanger={false} onChange={setPage} /> : null}
    </> : <Empty description="当前筛选条件下没有素材" />}

    <Modal title={previewAsset?.title || '素材预览'} open={Boolean(previewAsset)} onCancel={() => setPreviewAsset(null)} footer={null} width={720}>
      <div className="website-ads-library-preview">
        {previewAsset?.preview_url ? <video src={previewAsset.preview_url} poster={previewAsset.cover_url} controls autoPlay /> : previewAsset?.cover_url ? <img src={previewAsset.cover_url} alt="素材封面" /> : <Empty description="TikTok 暂未返回预览地址" />}
        <Descriptions size="small" column={1} bordered>
          <Descriptions.Item label="来源">{previewAsset?.source_label || '广告主素材库'}</Descriptions.Item>
          {previewAsset?.source === 'SPARK_AUTHORIZED_POST' ? <>
            <Descriptions.Item label="达人账号">{previewAsset.creator_name || '待同步'}</Descriptions.Item>
            <Descriptions.Item label="授权状态">{previewAsset.authorization_status || '待核验'}</Descriptions.Item>
            <Descriptions.Item label="授权有效期">{timeLabel(previewAsset.authorization_end_time)}</Descriptions.Item>
          </> : null}
          <Descriptions.Item label="关联商品">{productById.get(Number(previewAsset?.landing_page_id || previewAsset?.product_match?.landing_page_id))?.title || '尚未关联'}</Descriptions.Item>
          <Descriptions.Item label="Hermes 视频描述">{previewAsset?.video_description || '分析完成后自动生成'}</Descriptions.Item>
          <Descriptions.Item label="风险提示">
            <Space wrap>
              <Tag color={policyRiskMeta(previewAsset)[1]}>{policyRiskMeta(previewAsset)[0]}</Tag>
              <span>{(previewAsset?.policy_flags || []).join('；') || '未发现需要额外提示的风险'}</span>
            </Space>
          </Descriptions.Item>
          <Descriptions.Item label="自动投放状态"><Tag color={autoLaunchMeta(previewAsset)[1]}>{autoLaunchMeta(previewAsset)[0]}</Tag></Descriptions.Item>
          <Descriptions.Item label="下次自动检查">{previewAsset?.auto_launch_next_retry_at ? timeLabel(previewAsset.auto_launch_next_retry_at) : '按 5 分钟扫描周期自动检查'}</Descriptions.Item>
          <Descriptions.Item label="最近加入投放">{previewAsset?.auto_launched_at ? timeLabel(previewAsset.auto_launched_at) : '尚未加入'}</Descriptions.Item>
          <Descriptions.Item label="素材来源判断">{previewAsset?.production_origin || '待识别'}</Descriptions.Item>
          <Descriptions.Item label="素材 ID">{previewAsset?.tiktok_item_id || previewAsset?.video_id || '待同步'}</Descriptions.Item>
          <Descriptions.Item label="最近同步">{timeLabel(previewAsset?.last_synced_at)}</Descriptions.Item>
        </Descriptions>
      </div>
      {previewAsset?.contact_sheet_url ? <img className="website-ads-contact-sheet" src={previewAsset.contact_sheet_url} alt="视频关键画面" /> : null}
      {previewAsset?.transcript_excerpt ? <div className="website-ads-transcript"><strong>识别字幕</strong><p>{previewAsset.transcript_excerpt}</p></div> : null}
    </Modal>

    <Modal title="编辑素材档案" open={Boolean(editingAsset)} onCancel={() => setEditingAsset(null)} onOk={saveAsset} confirmLoading={saving} okText="保存并重新分析" width={640}>
      <Form form={assetForm} layout="vertical">
        <Form.Item name="title" label="素材标题" rules={[{ required: true, message: '请输入素材标题' }]}><Input /></Form.Item>
        <Form.Item name="landing_page_id" label="关联商品"><Select allowClear showSearch optionFilterProp="label" options={products.map((product) => ({ value: product.id, label: product.title }))} /></Form.Item>
        <Form.Item name="tags" label="人工标签"><Select mode="tags" tokenSeparators={[',']} placeholder="UGC、开箱、痛点、产品演示等" /></Form.Item>
        <Form.Item name="user_notes" label="素材说明"><Input.TextArea rows={5} placeholder="填写出镜人、核心话术、历史表现或需要避免的表述" /></Form.Item>
      </Form>
    </Modal>

    <Modal title="批量上传视频素材" open={uploadOpen} onCancel={() => { setUploadOpen(false); setUploadFiles([]); setUploadProgress(null); uploadForm.resetFields(); }} onOk={uploadAsset} confirmLoading={saving} okText={uploadMode === 'file' ? `上传 ${uploadFiles.length || ''}` : '上传到 TikTok'} width={640}>
      <Form form={uploadForm} layout="vertical">
        <Alert type="info" showIcon message="上传后自动完成素材档案" description="系统会提取字幕和关键画面，由 Hermes 自动生成视频描述、识别商品与真人/AIGC 来源；无需手动填写说明。" />
        <Segmented block value={uploadMode} onChange={setUploadMode} options={[{ value: 'file', label: '本地文件' }, { value: 'url', label: '视频 URL' }]} />
        {uploadMode === 'file' ? <Form.Item label="视频文件" required>
          <Upload.Dragger
            accept=".mp4,.mov,.mpeg,.avi,video/mp4,video/quicktime,video/mpeg,video/x-msvideo"
            multiple
            maxCount={20}
            fileList={uploadFiles}
            beforeUpload={() => false}
            onChange={({ fileList }) => {
              setUploadFiles(fileList.slice(-20));
            }}
          >
            <p className="ant-upload-text">点击选择或拖入多个视频</p>
            <p className="ant-upload-hint">单次最多 20 个；支持 MP4、MOV、MPEG、AVI，单文件最大 500 MB</p>
          </Upload.Dragger>
        </Form.Item> : <Form.Item name="video_url" label="可公开访问的视频 URL" rules={[{ required: true, type: 'url', message: '请输入有效的视频 URL' }]}><Input placeholder="https://.../video.mp4" /></Form.Item>}
        {uploadMode === 'url' ? <Form.Item name="file_name" label="素材名称" rules={[{ required: true, message: '请输入素材名称' }]}><Input maxLength={100} /></Form.Item> : null}
        {uploadProgress ? <Alert type={uploadProgress.failed ? 'warning' : 'info'} showIcon message={`处理进度 ${uploadProgress.completed}/${uploadProgress.total}`} description={`新增与重复文件会自动区分${uploadProgress.duplicates ? `，已忽略重复 ${uploadProgress.duplicates} 个` : ''}${uploadProgress.failed ? `，失败 ${uploadProgress.failed} 个` : ''}。`} /> : null}
      </Form>
    </Modal>
  </section>;
}
