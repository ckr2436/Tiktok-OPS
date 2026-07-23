import { useEffect, useMemo, useState } from 'react';
import { useParams } from 'react-router-dom';
import {
  Alert,
  Button,
  Checkbox,
  Drawer,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Radio,
  Segmented,
  Select,
  Space,
  Spin,
  Statistic,
  Switch,
  Table,
  Tag,
  Upload,
  message,
} from 'antd';

import { listAccounts } from '@/features/tenants/gmv_max/api/gmvMaxApi.js';
import {
  createConnection,
  createManualLandingPage,
  getWebsiteAdsMetadata,
  launchWebsiteAd,
  listConnections,
  listLandingPages,
  listAllWebsiteAdsCampaigns,
  listAllWebsiteAdsActions,
  runWebsiteAdsMonitor,
  searchInterests,
  searchLocations,
  syncConnection,
  syncWebsiteAdsCreativeAssets,
  updateWebsiteAdGroupDelivery,
  updateWebsiteAdStatus,
  updateWebsiteCampaignStatus,
  uploadWebsiteAdsVideoFile,
  uploadWebsiteAdsVideoByUrl,
  waitForWebsiteAdsVideoUploads,
} from './api.js';
import './WebsiteAdsPage.css';
import CreativeAssetLibrary from './CreativeAssetLibrary.jsx';
import HermesManagedLaunch from './HermesManagedLaunch.jsx';

const PROVIDER = 'tiktok-business';
const AGE_OPTIONS = [
  ['AGE_18_24', '18-24'],
  ['AGE_25_34', '25-34'],
  ['AGE_35_44', '35-44'],
  ['AGE_45_54', '45-54'],
  ['AGE_55_100', '55+'],
].map(([value, label]) => ({ value, label }));

const STATUS_META = {
  ACTIVE: ['投放中', 'success'],
  ENABLE: ['投放中', 'success'],
  PAUSED: ['已暂停', 'default'],
  DISABLE: ['已暂停', 'default'],
  CREATING: ['创建中', 'processing'],
  FAILED: ['创建失败', 'error'],
  DELETED: ['已删除', 'default'],
};

const ACTION_LABELS = {
  ENABLE_AD: '开启广告',
  PAUSE_AD: '暂停广告',
  ENABLE_CAMPAIGN: '开启系列',
  PAUSE_CAMPAIGN: '暂停系列',
  UPDATE_ADGROUP_DELIVERY: '调整预算或出价',
  AUTO_ADD_CREATIVE: '自动加入新素材',
  AUTO_CLONE_ADGROUP: '复制广告组扩量',
  AUTO_HOLD_CREATIVE: '暂缓新素材',
};

const ACTOR_META = {
  HERMES_GUARD: ['Hermes 守护', 'blue'],
  HERMES_ASSET_EXPANSION: ['Hermes 素材扩量', 'cyan'],
};

function itemsOf(payload) {
  if (!payload) return [];
  if (Array.isArray(payload)) return payload;
  for (const key of ['items', 'list', 'videos', 'identity_list', 'pixels', 'locations', 'interest_categories']) {
    if (Array.isArray(payload[key])) return payload[key];
  }
  if (payload.data) return itemsOf(payload.data);
  return [];
}

function accountLabel(item) {
  return item.label || item.account_name || item.alias || item.name || item.display_name || `授权账户 ${accountKey(item)}`;
}

function accountKey(item) {
  return item?.auth_id ?? item?.authId ?? item?.id;
}

function optionFrom(item, idKeys, labelKeys) {
  const value = idKeys.map((key) => item?.[key]).find(Boolean);
  if (!value) return null;
  const label = labelKeys.map((key) => item?.[key]).find(Boolean) || value;
  return { value: String(value), label: String(label), item };
}

function normalizedStatus(row) {
  const local = String(row?.status || '').toUpperCase();
  if (['FAILED', 'CREATING', 'DELETED'].includes(local)) return local;
  return String(row?.operation_status || local).toUpperCase();
}

function statusTag(row) {
  const normalized = normalizedStatus(row);
  const [label, color] = STATUS_META[normalized] || ['待同步', 'warning'];
  return <Tag color={color}>{label}</Tag>;
}

function money(value) {
  return `$${Number(value || 0).toFixed(2)}`;
}

function percent(value) {
  return `${(Number(value || 0) * 100).toFixed(2)}%`;
}

function genderLabel(value) {
  return {
    GENDER_FEMALE: '女性',
    GENDER_MALE: '男性',
    GENDER_UNLIMITED: '不限性别',
  }[value] || '不限性别';
}

function ageLabel(values) {
  const labels = Object.fromEntries(AGE_OPTIONS.map((item) => [item.value, item.label]));
  const selected = (Array.isArray(values) ? values : []).map((value) => labels[value]).filter(Boolean);
  return selected.length ? selected.join('、') : '不限年龄';
}

function shortId(value) {
  const text = String(value || '');
  return text.length > 12 ? `${text.slice(0, 6)}...${text.slice(-4)}` : text || '待同步';
}

function dateValue(offsetDays = 0) {
  const value = new Date();
  value.setDate(value.getDate() + offsetDays);
  return value.toISOString().slice(0, 10);
}

function dateValueInTimezone(timezoneName, offsetDays = 0) {
  try {
    const parts = new Intl.DateTimeFormat('en-US', {
      timeZone: timezoneName || 'UTC',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    }).formatToParts(new Date());
    const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
    const date = new Date(Date.UTC(Number(values.year), Number(values.month) - 1, Number(values.day) + offsetDays));
    return date.toISOString().slice(0, 10);
  } catch {
    return dateValue(offsetDays);
  }
}

function timeLabel(value) {
  if (!value) return '尚未同步';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString('zh-CN', { hour12: false });
}

function errorText(error, fallback) {
  const detail = error?.response?.data?.detail;
  if (typeof detail === 'string') return detail;
  return error?.message || fallback;
}

function campaignErrorText(value) {
  const text = String(value || '');
  if (!text) return '';
  if (text.includes('You must upload an image')) return '创建失败：视频封面缺失';
  if (text.includes('schedule_start_time')) return '创建失败：开始时间缺失';
  return text;
}

export default function WebsiteAdsPage() {
  const { wid } = useParams();
  const [launchForm] = Form.useForm();
  const [connectionForm] = Form.useForm();
  const [manualForm] = Form.useForm();
  const [deliveryForm] = Form.useForm();
  const [uploadForm] = Form.useForm();
  const bidStrategy = Form.useWatch('bid_strategy', launchForm);
  const guardEnabled = Form.useWatch('guard_enabled', launchForm);
  const selectedVideoId = Form.useWatch('video_id', launchForm);

  const [accounts, setAccounts] = useState([]);
  const [authId, setAuthId] = useState();
  const [connections, setConnections] = useState([]);
  const [landingPages, setLandingPages] = useState([]);
  const [campaigns, setCampaigns] = useState([]);
  const [actions, setActions] = useState([]);
  const [metadata, setMetadata] = useState({});
  const [selectedPage, setSelectedPage] = useState();
  const [selectedAdGroup, setSelectedAdGroup] = useState();
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [activeView, setActiveView] = useState('campaigns');
  const [launchOpen, setLaunchOpen] = useState(false);
  const [managedLaunchOpen, setManagedLaunchOpen] = useState(false);
  const [connectionOpen, setConnectionOpen] = useState(false);
  const [manualOpen, setManualOpen] = useState(false);
  const [deliveryOpen, setDeliveryOpen] = useState(false);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [uploadMode, setUploadMode] = useState('file');
  const [uploadFiles, setUploadFiles] = useState([]);
  const [uploadProgress, setUploadProgress] = useState(null);
  const [locationOptions, setLocationOptions] = useState([]);
  const [interestOptions, setInterestOptions] = useState([]);
  const [statusFilter, setStatusFilter] = useState('all');
  const [campaignSearch, setCampaignSearch] = useState('');
  const [assetSearch, setAssetSearch] = useState('');
  const [startDate, setStartDate] = useState(dateValue(-6));
  const [endDate, setEndDate] = useState(dateValue(0));
  const [lastLoadedAt, setLastLoadedAt] = useState();
  const [rangeTimezoneScope, setRangeTimezoneScope] = useState('');

  const pixelOptions = useMemo(
    () => itemsOf(metadata.pixels).map((item) => optionFrom(item, ['pixel_id', 'pixel_code', 'id'], ['pixel_name', 'name', 'pixel_id'])).filter(Boolean),
    [metadata],
  );
  const identityOptions = useMemo(
    () => itemsOf(metadata.identities).map((item) => {
      const source = item.identity_info || item;
      const option = optionFrom(source, ['identity_id', 'id'], ['display_name', 'username', 'user_name', 'identity_id']);
      if (!option) return null;
      return {
        ...option,
        label: option.label,
        identity_type: source.identity_type,
        identity_authorized_bc_id: source.identity_authorized_bc_id,
      };
    }).filter(Boolean),
    [metadata],
  );
  const videoOptions = useMemo(
    () => itemsOf(metadata.videos).map((item) => {
      const option = optionFrom(item, ['video_id', 'material_id', 'id'], ['file_name', 'video_name', 'name', 'video_id']);
      if (!option) return null;
      return {
        ...option,
        preview_url: item.preview_url,
        cover_url: item.video_cover_url,
        duration: item.duration,
        width: item.width,
        height: item.height,
      };
    }).filter(Boolean),
    [metadata],
  );
  const selectedVideo = useMemo(
    () => videoOptions.find((item) => item.value === String(selectedVideoId || '')),
    [videoOptions, selectedVideoId],
  );

  const filteredCampaigns = useMemo(() => campaigns.filter((campaign) => {
    const search = campaignSearch.trim().toLowerCase();
    const matchesSearch = !search || campaign.name?.toLowerCase().includes(search) || campaign.landing_page?.title?.toLowerCase().includes(search);
    const status = normalizedStatus(campaign);
    const matchesStatus = statusFilter === 'all'
      || (statusFilter === 'active' && ['ACTIVE', 'ENABLE'].includes(status))
      || (statusFilter === 'paused' && ['PAUSED', 'DISABLE'].includes(status))
      || (statusFilter === 'failed' && status === 'FAILED');
    return matchesSearch && matchesStatus;
  }), [campaigns, campaignSearch, statusFilter]);

  const filteredLandingPages = useMemo(() => landingPages.filter((page) => {
    const search = assetSearch.trim().toLowerCase();
    return !search || page.title?.toLowerCase().includes(search) || page.landing_url?.toLowerCase().includes(search);
  }), [landingPages, assetSearch]);

  const summary = useMemo(() => campaigns.reduce((total, campaign) => {
    total.spend += Number(campaign.metrics?.spend || 0);
    total.impressions += Number(campaign.metrics?.impressions || 0);
    total.clicks += Number(campaign.metrics?.clicks || 0);
    total.videoPlayActions += Number(campaign.metrics?.video_play_actions || 0);
    total.videoWatched6s += Number(campaign.metrics?.video_watched_6s || 0);
    total.conversions += Number(campaign.metrics?.conversions || 0);
    if (['ACTIVE', 'ENABLE'].includes(normalizedStatus(campaign))) total.active += 1;
    return total;
  }, { spend: 0, impressions: 0, clicks: 0, videoPlayActions: 0, videoWatched6s: 0, conversions: 0, active: 0 }), [campaigns]);

  async function loadAccounts() {
    const payload = await listAccounts(wid, PROVIDER, { page_size: 50 });
    const rows = itemsOf(payload);
    setAccounts(rows);
    setAuthId((current) => current || accountKey(rows[0]));
  }

  async function loadWorkspaceData(targetAuthId = authId, { quiet = false } = {}) {
    if (!targetAuthId) return;
    if (!quiet) setRefreshing(true);
    try {
      const [connectionPayload, landingPayload, campaignPayload, actionPayload] = await Promise.all([
        listConnections(wid, PROVIDER, targetAuthId),
        listLandingPages(wid, PROVIDER, targetAuthId),
        listAllWebsiteAdsCampaigns(wid, PROVIDER, targetAuthId, {
          page_size: 100,
          start_date: startDate,
          end_date: endDate,
        }),
        listAllWebsiteAdsActions(wid, PROVIDER, targetAuthId, { page_size: 200 }),
      ]);
      setConnections(itemsOf(connectionPayload));
      setLandingPages(itemsOf(landingPayload));
      setCampaigns(itemsOf(campaignPayload));
      setActions(itemsOf(actionPayload));
      try {
        const loadedMetadata = await getWebsiteAdsMetadata(wid, PROVIDER, targetAuthId);
        setMetadata(loadedMetadata);
        const timezoneName = loadedMetadata.advertiser_timezone || 'UTC';
        const timezoneScope = `${targetAuthId}:${timezoneName}`;
        if (rangeTimezoneScope !== timezoneScope) {
          setRangeTimezoneScope(timezoneScope);
          setStartDate(dateValueInTimezone(timezoneName, -6));
          setEndDate(dateValueInTimezone(timezoneName, 0));
        }
      } catch (error) {
        setMetadata({});
        message.warning(errorText(error, 'TikTok 素材元数据暂时无法加载'));
      }
      setLastLoadedAt(new Date());
    } finally {
      if (!quiet) setRefreshing(false);
    }
  }

  useEffect(() => {
    setLoading(true);
    loadAccounts().catch((error) => message.error(errorText(error, '授权账户加载失败'))).finally(() => setLoading(false));
  }, [wid]);

  useEffect(() => {
    if (!authId) return;
    setLoading(true);
    loadWorkspaceData(authId, { quiet: true })
      .catch((error) => message.error(errorText(error, '独立站广告数据加载失败')))
      .finally(() => setLoading(false));
  }, [authId, startDate, endDate]);

  async function submitConnection() {
    const values = await connectionForm.validateFields();
    setSubmitting(true);
    try {
      const connection = await createConnection(wid, PROVIDER, authId, values);
      await syncConnection(wid, PROVIDER, authId, connection.id);
      message.success('Magento 已连接，落地页同步完成');
      setConnectionOpen(false);
      connectionForm.resetFields();
      await loadWorkspaceData();
    } catch (error) {
      message.error(errorText(error, 'Magento 连接失败'));
    } finally {
      setSubmitting(false);
    }
  }

  async function submitManualPage() {
    const values = await manualForm.validateFields();
    setSubmitting(true);
    try {
      await createManualLandingPage(wid, PROVIDER, authId, values);
      message.success('落地页已添加');
      setManualOpen(false);
      manualForm.resetFields();
      await loadWorkspaceData();
    } catch (error) {
      message.error(errorText(error, '落地页添加失败'));
    } finally {
      setSubmitting(false);
    }
  }

  function openLaunch(page) {
    setSelectedPage(page);
    const suffix = new Date().toISOString().slice(0, 10).replaceAll('-', '');
    launchForm.setFieldsValue({
      landing_page_id: page.id,
      campaign_name: `${page.title.slice(0, 40)} Website Sales ${suffix}`,
      adgroup_name: `${page.title.slice(0, 40)} 精准定向`,
      ad_name: `${page.title.slice(0, 40)} 视频广告`,
      call_to_action: 'SHOP_NOW',
      daily_budget: 50,
      bid_strategy: 'LOWEST_COST',
      gender: 'GENDER_UNLIMITED',
      age_groups: AGE_OPTIONS.map((item) => item.value),
      placement_type: 'PLACEMENT_TYPE_NORMAL',
      location_ids: ['6252001'],
      activate_after_create: false,
      guard_enabled: true,
      max_unprofitable_spend: undefined,
      min_ctr_percent: 4,
      max_cpc: 0.30,
      min_impressions_before_action: 100,
      min_clicks_for_cpc: 3,
      min_spend_before_action: 0.90,
      min_runtime_minutes: 0,
    });
    setLocationOptions((current) => {
      const us = { value: '6252001', label: 'United States · US', item: { geo_type: 'COUNTRY', region_code: 'US' } };
      return current.some((item) => item.value === us.value) ? current : [us, ...current];
    });
    setLaunchOpen(true);
  }

  function openManagedLaunch(page) {
    setSelectedPage(page);
    setManagedLaunchOpen(true);
  }

  async function submitLaunch() {
    const values = await launchForm.validateFields();
    const identity = identityOptions.find((item) => item.value === String(values.identity_id));
    setSubmitting(true);
    try {
      await launchWebsiteAd(wid, PROVIDER, authId, {
        landing_page_id: values.landing_page_id,
        campaign_name: values.campaign_name,
        adgroup_name: values.adgroup_name,
        ad_name: values.ad_name,
        pixel_id: values.pixel_id,
        identity_type: identity?.identity_type,
        identity_id: values.identity_id,
        identity_authorized_bc_id: identity?.identity_authorized_bc_id || undefined,
        video_id: values.video_id,
        ad_text: values.ad_text,
        call_to_action: values.call_to_action,
        daily_budget: values.daily_budget,
        bid_strategy: values.bid_strategy,
        conversion_bid_price: values.conversion_bid_price,
        targeting: {
          location_ids: values.location_ids,
          gender: values.gender,
          age_groups: values.age_groups,
          interest_category_ids: values.interest_category_ids || [],
          audience_ids: values.audience_ids || [],
          excluded_audience_ids: values.excluded_audience_ids || [],
          placement_type: values.placement_type,
          placements: ['PLACEMENT_TIKTOK'],
        },
        guard: {
          enabled: values.guard_enabled,
          max_unprofitable_spend: values.max_unprofitable_spend,
          min_ctr: Number(values.min_ctr_percent || 4) / 100,
          max_cpc: values.max_cpc,
          min_impressions_before_action: values.min_impressions_before_action,
          min_clicks_for_cpc: values.min_clicks_for_cpc,
          min_spend_before_action: values.min_spend_before_action,
          min_runtime_minutes: 0,
          pause_minutes: values.pause_minutes || 60,
        },
        activate_after_create: values.activate_after_create,
      });
      message.success(values.activate_after_create ? '广告已创建并开始投放' : '广告已安全创建，当前保持暂停');
      setLaunchOpen(false);
      launchForm.resetFields();
      setActiveView('campaigns');
      await loadWorkspaceData();
    } catch (error) {
      message.error(errorText(error, '广告创建失败'));
    } finally {
      setSubmitting(false);
    }
  }

  async function submitUpload() {
    const values = uploadMode === 'url' ? await uploadForm.validateFields() : {};
    const pendingFiles = uploadFiles.filter((item) => item.originFileObj);
    if (uploadMode === 'file' && !pendingFiles.length) {
      message.warning('请选择一个或多个本地视频文件');
      return;
    }
    setSubmitting(true);
    setUploadProgress(uploadMode === 'file' ? { completed: 0, total: pendingFiles.length, failed: 0, duplicates: 0 } : null);
    try {
      const results = [];
      const failures = [];
      if (uploadMode === 'file') {
        for (const item of pendingFiles) {
          try {
            const sourceFile = item.originFileObj;
            const formData = new FormData();
            formData.append('video_file', sourceFile);
            formData.append('file_name', sourceFile.name.slice(0, 100));
            formData.append('flaw_detect', 'false');
            formData.append('auto_fix_enabled', 'false');
            // eslint-disable-next-line no-await-in-loop
            const response = await uploadWebsiteAdsVideoFile(wid, PROVIDER, authId, formData);
            results.push({ response, name: sourceFile.name });
          } catch (error) {
            failures.push({ item, error });
          }
          setUploadProgress({
            completed: results.length + failures.length,
            total: pendingFiles.length,
            failed: failures.length,
            duplicates: results.filter(({ response }) => response?.deduplicated || response?.skipped).length,
          });
        }
      } else {
        const response = await uploadWebsiteAdsVideoByUrl(wid, PROVIDER, authId, {
          video_url: values.video_url,
          file_name: values.file_name,
        });
        results.push({ response, name: values.file_name });
      }
      if (!results.length) {
        setUploadFiles(failures.map(({ item }) => item));
        message.error('本批次视频均上传失败，请检查文件后重试');
        return;
      }
      const backgroundUploadIds = [...new Set(results
        .filter(({ response }) => response?.in_progress && response?.upload_id)
        .map(({ response }) => Number(response.upload_id)))];
      const completedResults = results.filter(({ response }) => !response?.in_progress);
      const duplicateCount = completedResults.filter(({ response }) => response?.deduplicated || response?.skipped).length;
      const newCount = completedResults.length - duplicateCount;
      if (completedResults.length) {
        await syncWebsiteAdsCreativeAssets(wid, PROVIDER, authId);
        const loadedMetadata = await getWebsiteAdsMetadata(wid, PROVIDER, authId);
        setMetadata(loadedMetadata);
        const firstResponse = completedResults.find(({ response }) => response?.video_id || response?.data?.video_id)?.response || completedResults[0].response;
        const firstData = firstResponse?.data || firstResponse || {};
        const firstVideoId = firstData.video_id || firstData.material_id;
        if (firstVideoId) launchForm.setFieldValue('video_id', String(firstVideoId));
      }
      if (backgroundUploadIds.length) {
        const notificationKey = `website-upload-${backgroundUploadIds.join('-')}`;
        message.open({ key: notificationKey, type: 'loading', duration: 0, content: `已落盘 ${backgroundUploadIds.length} 个视频，正在后台上传 TikTok` });
        void waitForWebsiteAdsVideoUploads(wid, PROVIDER, authId, backgroundUploadIds, {
          onProgress: ({ completed: done, failed, total }) => {
            message.open({ key: notificationKey, type: 'loading', duration: 0, content: `后台上传 ${done + failed}/${total}，可继续创建其他内容` });
          },
        }).then(async ({ items, completed: done, failed, total }) => {
          await syncWebsiteAdsCreativeAssets(wid, PROVIDER, authId);
          const loadedMetadata = await getWebsiteAdsMetadata(wid, PROVIDER, authId);
          setMetadata(loadedMetadata);
          const firstUploaded = items.find((item) => item.video_id || item.data?.video_id);
          const firstVideoId = firstUploaded?.video_id || firstUploaded?.data?.video_id;
          if (firstVideoId) launchForm.setFieldValue('video_id', String(firstVideoId));
          message.open({
            key: notificationKey,
            type: failed ? 'warning' : 'success',
            duration: 6,
            content: failed ? `后台上传完成 ${done}/${total}，失败 ${failed} 个` : `后台上传完成，共新增 ${done} 个视频`,
          });
        }).catch((error) => {
          message.open({ key: notificationKey, type: 'info', duration: 6, content: errorText(error, '后台仍在继续上传，可稍后刷新素材库查看') });
        });
      }
      if (failures.length) {
        setUploadFiles(failures.map(({ item }) => item));
        message.warning(`新增 ${newCount} 个，自动忽略重复 ${duplicateCount} 个，失败 ${failures.length} 个；失败文件已保留，可直接重试`);
      } else {
        if (backgroundUploadIds.length) message.success(`已提交 ${backgroundUploadIds.length} 个后台上传任务，页面可正常继续使用`);
        else if (newCount) message.success(`新增 ${newCount} 个视频，自动忽略重复 ${duplicateCount} 个；Hermes 正在分析新素材`);
        else message.info(`所选 ${duplicateCount} 个视频均已存在，系统已自动忽略，未重复上传`);
        setUploadOpen(false);
        uploadForm.resetFields();
        setUploadFiles([]);
        setUploadProgress(null);
      }
    } catch (error) {
      message.error(errorText(error, '视频上传失败，请确认 URL 可被 TikTok 公网访问'));
    } finally {
      setSubmitting(false);
    }
  }

  function openDelivery(campaign) {
    const group = campaign.adgroups?.[0];
    if (!group) return;
    setSelectedAdGroup(group);
    deliveryForm.setFieldsValue({ daily_budget: group.budget, conversion_bid_price: group.conversion_bid_price });
    setDeliveryOpen(true);
  }

  async function submitDelivery() {
    const values = await deliveryForm.validateFields();
    setSubmitting(true);
    try {
      await updateWebsiteAdGroupDelivery(wid, PROVIDER, authId, selectedAdGroup.id, values);
      message.success('预算与出价已更新');
      setDeliveryOpen(false);
      await loadWorkspaceData();
    } catch (error) {
      message.error(errorText(error, '投放参数更新失败'));
    } finally {
      setSubmitting(false);
    }
  }

  async function findLocations(keyword) {
    if (!keyword || keyword.trim().length < 2) return;
    try {
      const payload = await searchLocations(wid, PROVIDER, authId, { search_keyword: keyword.trim() });
      setLocationOptions(itemsOf(payload).map((item) => {
        const option = optionFrom(item, ['location_id', 'id'], ['name', 'location_name', 'location_id']);
        return option ? { ...option, label: `${option.label}${item.region_code ? ` · ${item.region_code}` : ''}` } : null;
      }).filter(Boolean));
    } catch (error) {
      message.warning(errorText(error, '地域搜索失败'));
    }
  }

  async function findInterests(keyword) {
    if (!keyword || keyword.trim().length < 2) return;
    try {
      const payload = await searchInterests(wid, PROVIDER, authId, { search_keywords: [keyword.trim()] });
      const categoryLabels = { general_interest: '兴趣', purchase_intention: '购买意向', video_interaction: '视频互动', creator_interaction: '创作者互动', additional_interest: '扩展兴趣' };
      setInterestOptions(itemsOf(payload).map((item) => {
        const option = optionFrom(item, ['interest_category_id', 'id'], ['name', 'interest_category_name', 'id']);
        return option ? { ...option, label: `${option.label} · ${categoryLabels[item.category] || '兴趣'}` } : null;
      }).filter(Boolean));
    } catch (error) {
      message.warning(errorText(error, '兴趣搜索失败'));
    }
  }

  async function changeCampaignStatus(campaign) {
    const enabled = ['ACTIVE', 'ENABLE'].includes(normalizedStatus(campaign));
    try {
      await updateWebsiteCampaignStatus(wid, PROVIDER, authId, campaign.id, enabled ? 'DISABLE' : 'ENABLE', '独立站广告控制台人工操作');
      message.success(enabled ? '系列已暂停' : '系列已开启');
      await loadWorkspaceData();
    } catch (error) {
      message.error(errorText(error, enabled ? '系列暂停失败' : '系列开启失败'));
    }
  }

  const adColumns = [
    { title: '广告', dataIndex: 'name', key: 'name', width: 260, render: (value, row) => <div className="website-ads-primary-cell"><strong>{value}</strong><span>ID {shortId(row.ad_id)} · 视频 {shortId(row.video_id)}</span></div> },
    { title: '状态', key: 'status', width: 90, render: (_, row) => statusTag(row) },
    { title: '消耗', key: 'spend', width: 90, render: (_, row) => money(row.metrics?.spend) },
    { title: '曝光', key: 'impressions', width: 82, render: (_, row) => Number(row.metrics?.impressions || 0).toLocaleString() },
    { title: '点击', key: 'clicks', width: 72, render: (_, row) => Number(row.metrics?.clicks || 0) },
    { title: 'CTR', key: 'ctr', width: 82, render: (_, row) => percent(row.metrics?.ctr) },
    { title: 'CPC', key: 'cpc', width: 82, render: (_, row) => money(row.metrics?.cpc) },
    { title: 'CPM', key: 'cpm', width: 82, render: (_, row) => money(row.metrics?.cpm) },
    { title: '视频播放', key: 'videoPlay', width: 92, render: (_, row) => Number(row.metrics?.video_play_actions || 0).toLocaleString() },
    { title: '2秒观看率', key: 'video2s', width: 98, render: (_, row) => percent(row.metrics?.video_2s_rate) },
    { title: '6秒观看率', key: 'video6s', width: 98, render: (_, row) => percent(row.metrics?.video_6s_rate) },
    { title: '25%播放率', key: 'video25', width: 102, render: (_, row) => percent(row.metrics?.video_p25_rate) },
    { title: '50%播放率', key: 'video50', width: 102, render: (_, row) => percent(row.metrics?.video_p50_rate) },
    { title: '75%播放率', key: 'video75', width: 102, render: (_, row) => percent(row.metrics?.video_p75_rate) },
    { title: '完播率', key: 'completion', width: 86, render: (_, row) => percent(row.metrics?.video_completion_rate) },
    { title: '浏览内容', key: 'conversion', width: 88, render: (_, row) => Number(row.metrics?.conversions || 0) },
    { title: '守护', key: 'guard', width: 90, render: (_, row) => row.guard_enabled ? <Tag color="blue">已开启</Tag> : <Tag>未开启</Tag> },
    {
      title: '操作', key: 'actions', fixed: 'right', width: 92, render: (_, row) => {
        const enabled = normalizedStatus(row) === 'ENABLE';
        return <Popconfirm title={enabled ? '暂停这条广告？' : '开启这条广告？'} onConfirm={async () => {
          try {
            await updateWebsiteAdStatus(wid, PROVIDER, authId, row.id, enabled ? 'DISABLE' : 'ENABLE', '独立站广告控制台人工操作');
            message.success(enabled ? '广告已暂停' : '广告已开启');
            await loadWorkspaceData();
          } catch (error) {
            message.error(errorText(error, '广告状态更新失败'));
          }
        }}><Button size="small">{enabled ? '暂停' : '开启'}</Button></Popconfirm>;
      },
    },
  ];

  const audienceColumns = [
    {
      title: '精准定向组', key: 'audience', width: 230, render: (_, row) => <div className="website-ads-primary-cell">
        <strong>{row.audience_segment || row.name}</strong>
        <span>{row.targeting?.targeting_rationale || row.name}</span>
      </div>,
    },
    {
      title: '定向条件', key: 'targeting', width: 330, render: (_, row) => <div className="website-ads-primary-cell">
        <strong>{(row.targeting?.interest_keywords || []).join('、') || `兴趣 ID ${(row.targeting?.interest_category_ids || []).join('、')}`}</strong>
        <span>{genderLabel(row.targeting?.gender)} · {ageLabel(row.targeting?.age_groups)}</span>
      </div>,
    },
    { title: '状态', key: 'status', width: 88, render: (_, row) => statusTag(row) },
    { title: '预算', key: 'budget', width: 82, render: (_, row) => money(row.budget) },
    { title: '消耗', key: 'spend', width: 82, sorter: (a, b) => Number(a.metrics?.spend || 0) - Number(b.metrics?.spend || 0), render: (_, row) => money(row.metrics?.spend) },
    { title: '曝光', key: 'impressions', width: 82, render: (_, row) => Number(row.metrics?.impressions || 0).toLocaleString() },
    { title: '点击', key: 'clicks', width: 68, sorter: (a, b) => Number(a.metrics?.clicks || 0) - Number(b.metrics?.clicks || 0), render: (_, row) => Number(row.metrics?.clicks || 0) },
    { title: 'CTR', key: 'ctr', width: 78, sorter: (a, b) => Number(a.metrics?.ctr || 0) - Number(b.metrics?.ctr || 0), render: (_, row) => percent(row.metrics?.ctr) },
    { title: 'CPC', key: 'cpc', width: 78, sorter: (a, b) => Number(a.metrics?.cpc || 0) - Number(b.metrics?.cpc || 0), render: (_, row) => money(row.metrics?.cpc) },
    { title: '视频播放', key: 'videoPlay', width: 92, render: (_, row) => Number(row.metrics?.video_play_actions || 0).toLocaleString() },
    { title: '2秒观看率', key: 'video2s', width: 98, render: (_, row) => percent(row.metrics?.video_2s_rate) },
    { title: '6秒观看率', key: 'video6s', width: 98, render: (_, row) => percent(row.metrics?.video_6s_rate) },
    { title: '完播率', key: 'completion', width: 86, render: (_, row) => percent(row.metrics?.video_completion_rate) },
  ];

  const campaignColumns = [
    { title: '系列与落地页', key: 'campaign', width: 350, render: (_, row) => <div className="website-ads-campaign-cell">{row.landing_page?.image_url ? <img src={row.landing_page.image_url} alt="" /> : <div className="website-ads-mini-placeholder">无图</div>}<div className="website-ads-primary-cell"><strong>{row.name}</strong><span>{row.landing_page?.title || '落地页待同步'} · ID {shortId(row.campaign_id)}</span></div></div> },
    { title: '状态', key: 'status', width: 100, render: (_, row) => statusTag(row) },
    { title: '消耗', key: 'spend', width: 100, sorter: (a, b) => Number(a.metrics?.spend || 0) - Number(b.metrics?.spend || 0), render: (_, row) => money(row.metrics?.spend) },
    { title: '曝光', key: 'impressions', width: 88, sorter: (a, b) => Number(a.metrics?.impressions || 0) - Number(b.metrics?.impressions || 0), render: (_, row) => Number(row.metrics?.impressions || 0).toLocaleString() },
    { title: '点击', key: 'clicks', width: 72, sorter: (a, b) => Number(a.metrics?.clicks || 0) - Number(b.metrics?.clicks || 0), render: (_, row) => Number(row.metrics?.clicks || 0) },
    { title: 'CTR', key: 'ctr', width: 82, sorter: (a, b) => Number(a.metrics?.ctr || 0) - Number(b.metrics?.ctr || 0), render: (_, row) => percent(row.metrics?.ctr) },
    { title: 'CPC', key: 'cpc', width: 82, render: (_, row) => money(row.metrics?.cpc) },
    { title: 'CPM', key: 'cpm', width: 82, render: (_, row) => money(row.metrics?.cpm) },
    { title: '视频播放', key: 'videoPlay', width: 92, sorter: (a, b) => Number(a.metrics?.video_play_actions || 0) - Number(b.metrics?.video_play_actions || 0), render: (_, row) => Number(row.metrics?.video_play_actions || 0).toLocaleString() },
    { title: '2秒观看率', key: 'video2s', width: 98, render: (_, row) => percent(row.metrics?.video_2s_rate) },
    { title: '6秒观看率', key: 'video6s', width: 98, render: (_, row) => percent(row.metrics?.video_6s_rate) },
    { title: '完播率', key: 'completion', width: 86, render: (_, row) => percent(row.metrics?.video_completion_rate) },
    { title: '浏览内容', key: 'conversions', width: 88, sorter: (a, b) => Number(a.metrics?.conversions || 0) - Number(b.metrics?.conversions || 0), render: (_, row) => Number(row.metrics?.conversions || 0) },
    { title: '同步状态', key: 'sync', width: 150, render: (_, row) => <div className="website-ads-primary-cell"><span>{timeLabel(row.last_metrics_sync_at)}</span>{row.error_message ? <span className="website-ads-error-text">{campaignErrorText(row.error_message)}</span> : <span>广告级数据</span>}</div> },
    {
      title: '操作', key: 'actions', fixed: 'right', width: 128, render: (_, row) => {
        const enabled = ['ACTIVE', 'ENABLE'].includes(normalizedStatus(row));
        const incomplete = normalizedStatus(row) === 'FAILED' || !row.ads?.length;
        return <Space size={4}>
          <Popconfirm title={enabled ? '确认暂停整个系列？' : '确认开启整个系列及其广告？'} onConfirm={() => changeCampaignStatus(row)}><Button size="small" disabled={incomplete}>{enabled ? '暂停' : '开启'}</Button></Popconfirm>
          <Button size="small" disabled={!row.adgroups?.length} onClick={() => openDelivery(row)}>调参</Button>
        </Space>;
      },
    },
  ];

  const actionColumns = [
    { title: '时间', dataIndex: 'created_at', key: 'created_at', width: 180, render: timeLabel },
    { title: '执行者', dataIndex: 'actor_type', key: 'actor_type', width: 150, render: (value) => { const meta = ACTOR_META[value] || ['人工操作', 'default']; return <Tag color={meta[1]}>{meta[0]}</Tag>; } },
    { title: '动作', dataIndex: 'action', key: 'action', width: 160, render: (value) => ACTION_LABELS[value] || value },
    { title: '原因', dataIndex: 'reason', key: 'reason', ellipsis: true, render: (value) => value || '未填写原因' },
    { title: '结果', dataIndex: 'result', key: 'result', width: 90, render: (value) => { const meta = value === 'SUCCESS' ? ['成功', 'success'] : value === 'SKIPPED' ? ['暂缓', 'warning'] : ['失败', 'error']; return <Tag color={meta[1]}>{meta[0]}</Tag>; } },
  ];

  if (loading && !authId) return <div className="website-ads-page website-ads-loading"><Spin size="large" /></div>;

  return (
    <main className="website-ads-page">
      <header className="website-ads-header">
        <div className="website-ads-title-block">
          <div className="website-ads-title-row"><h1>独立站广告</h1><Tag color="blue">Web Link · 优质点击</Tag></div>
          <p>以精准定向获取优质点击，由 Hermes 设计受众实验、分配真实达人素材并按 CTR 与 CPC 执行广告级止损。</p>
        </div>
        <div className="website-ads-header-actions">
          <Select className="website-ads-account-select" value={authId} onChange={setAuthId} options={accounts.map((item) => ({ value: accountKey(item), label: accountLabel(item) })).filter((item) => item.value != null)} placeholder="选择 TikTok 授权账户" />
          <Button loading={refreshing} onClick={() => loadWorkspaceData()}>刷新</Button>
          <Button type="primary" disabled={!landingPages.length} onClick={() => openManagedLaunch(landingPages[0])}>Hermes 托管投放</Button>
        </div>
      </header>

      <section className="website-ads-summary" aria-label="投放概览">
        <Statistic title="消耗" value={summary.spend} precision={2} prefix="$" />
        <Statistic title="曝光" value={summary.impressions} precision={0} />
        <Statistic title="点击" value={summary.clicks} precision={0} />
        <Statistic title="CTR" value={summary.impressions ? summary.clicks * 100 / summary.impressions : 0} precision={2} suffix="%" />
        <Statistic title="CPC" value={summary.clicks ? summary.spend / summary.clicks : 0} precision={2} prefix="$" />
        <Statistic title="视频播放" value={summary.videoPlayActions} precision={0} />
        <Statistic title="6秒观看率" value={summary.videoPlayActions ? summary.videoWatched6s * 100 / summary.videoPlayActions : 0} precision={2} suffix="%" />
        <Statistic title="投放中系列" value={summary.active} suffix={`/ ${campaigns.length}`} />
      </section>

      <section className="website-ads-controlbar">
        <Segmented value={activeView} onChange={setActiveView} options={[
          { value: 'campaigns', label: '投放管理' },
          { value: 'assets', label: `商品库 ${landingPages.length}` },
          { value: 'creatives', label: '素材管理' },
          { value: 'actions', label: '操作审计' },
        ]} />
        <div className="website-ads-freshness">自动监控每 3 分钟执行 · 页面更新于 {lastLoadedAt ? lastLoadedAt.toLocaleTimeString('zh-CN', { hour12: false }) : '加载中'}</div>
      </section>

      {connections.some((item) => item.last_error) && <Alert type="warning" showIcon message="Magento 最近一次同步失败" description={connections.find((item) => item.last_error)?.last_error} />}

      {activeView === 'campaigns' && <section className="website-ads-panel">
        <div className="website-ads-section-head">
          <div><h2>投放管理</h2><p>{startDate} 至 {endDate}，按广告主时区 {metadata.advertiser_timezone || '加载中'} 聚合。</p></div>
          <div className="website-ads-filters">
            <div className="website-ads-date-range"><Input aria-label="开始日期" type="date" value={startDate} max={endDate} onChange={(event) => setStartDate(event.target.value)} /><span>至</span><Input aria-label="结束日期" type="date" value={endDate} min={startDate} onChange={(event) => setEndDate(event.target.value)} /></div>
            <Select value={statusFilter} onChange={setStatusFilter} options={[{ value: 'all', label: '全部状态' }, { value: 'active', label: '投放中' }, { value: 'paused', label: '已暂停' }, { value: 'failed', label: '创建失败' }]} />
            <Input.Search allowClear value={campaignSearch} onChange={(event) => setCampaignSearch(event.target.value)} placeholder="搜索系列或落地页" />
            <Popconfirm title="立即运行广告监控？" onConfirm={async () => {
              try {
                const result = await runWebsiteAdsMonitor(wid, PROVIDER, authId);
                message.success(`检查 ${result.ads || 0} 个广告，暂停 ${result.paused || 0} 个`);
                await loadWorkspaceData();
              } catch (error) {
                message.error(errorText(error, '监控任务执行失败'));
              }
            }}><Button>立即巡检</Button></Popconfirm>
          </div>
        </div>
        <Table
          rowKey="id"
          loading={loading}
          columns={campaignColumns}
          dataSource={filteredCampaigns}
          pagination={{ pageSize: 20, showSizeChanger: false }}
          scroll={{ x: 1750 }}
          expandable={{
            rowExpandable: (row) => Boolean(row.ads?.length || row.adgroups?.length),
            expandedRowRender: (row) => <div className="website-ads-expanded">
              <div className="website-ads-expanded-head"><strong>定向组表现</strong><span>目标 CTR ≥ 4% · CPC ≤ $0.30，各组数据独立记录</span></div>
              <Table rowKey="id" size="small" columns={audienceColumns} dataSource={row.adgroups || []} pagination={false} scroll={{ x: 1550 }} />
              <div className="website-ads-expanded-head"><strong>广告素材表现</strong><span>开始消耗并达到有效样本后，Hermes 按点击质量独立评估每条广告</span></div>
              <Table rowKey="id" size="small" columns={adColumns} dataSource={row.ads || []} pagination={false} scroll={{ x: 2050 }} />
            </div>,
          }}
          locale={{ emptyText: <Empty description="当前筛选条件下暂无独立站广告" /> }}
        />
      </section>}

      {activeView === 'assets' && <section className="website-ads-panel">
        <div className="website-ads-section-head">
          <div><h2>商品库</h2><p>维护真实卖家、成交价、促销与产品资料，Hermes 会据此分析受众和广告角度。</p></div>
          <div className="website-ads-filters">
            <Input.Search allowClear value={assetSearch} onChange={(event) => setAssetSearch(event.target.value)} placeholder="搜索名称或 URL" />
            {connections.map((connection) => <Button key={connection.id} onClick={async () => {
              try {
                await syncConnection(wid, PROVIDER, authId, connection.id);
                message.success(`${connection.name} 同步完成`);
                await loadWorkspaceData();
              } catch (error) {
                message.error(errorText(error, 'Magento 同步失败'));
              }
            }}>同步 Magento</Button>)}
            <Button onClick={() => setManualOpen(true)}>添加链接</Button>
            {!connections.length && <Button type="primary" onClick={() => setConnectionOpen(true)}>连接 Magento</Button>}
          </div>
        </div>
        {filteredLandingPages.length ? <div className="website-ads-assets">{filteredLandingPages.map((page) => <article key={page.id} className="website-ads-asset-card">
          {page.image_url ? <img src={page.image_url} alt="" /> : <div className="website-ads-image-placeholder">无图</div>}
          <div className="website-ads-asset-content">
            <div className="website-ads-asset-title"><strong>{page.title}</strong><Tag>{page.connection_id ? 'Magento' : '手工'}</Tag></div>
            <a href={page.landing_url} target="_blank" rel="noreferrer" className="website-ads-url">{page.landing_url}</a>
            <div className="website-ads-asset-meta"><span>{page.reference_price != null ? `${page.currency} ${Number(page.reference_price).toFixed(2)}` : '未设置参考价'}</span><span>同步于 {timeLabel(page.last_synced_at)}</span></div>
          </div>
          <Space>
            <Button type="primary" onClick={() => openManagedLaunch(page)}>Hermes 托管投放</Button>
            <Button onClick={() => openLaunch(page)}>高级手动</Button>
          </Space>
        </article>)}</div> : <Empty description="连接 Magento 并同步落地页后即可创建广告" />}
      </section>}

      {activeView === 'creatives' && <CreativeAssetLibrary
        workspaceId={wid}
        provider={PROVIDER}
        authId={authId}
        products={landingPages}
      />}

      {activeView === 'actions' && <section className="website-ads-panel">
        <div className="website-ads-section-head"><div><h2>操作审计</h2><p>记录人工操作与 Hermes 守护动作，便于复盘和追责。</p></div></div>
        <Table rowKey="id" columns={actionColumns} dataSource={actions} pagination={{ pageSize: 20, showSizeChanger: false }} scroll={{ x: 780 }} locale={{ emptyText: '暂无操作记录' }} />
      </section>}

      <HermesManagedLaunch
        open={managedLaunchOpen}
        onClose={() => setManagedLaunchOpen(false)}
        workspaceId={wid}
        provider={PROVIDER}
        authId={authId}
        products={landingPages}
        initialProduct={selectedPage}
        onProductUpdated={(updated) => {
          setLandingPages((current) => current.map((item) => (item.id === updated.id ? updated : item)));
          setSelectedPage(updated);
        }}
        onCompleted={async () => {
          setActiveView('campaigns');
          await loadWorkspaceData();
        }}
      />

      <Drawer title={selectedPage ? `创建投放 · ${selectedPage.title}` : '创建投放'} open={launchOpen} onClose={() => setLaunchOpen(false)} width={720} extra={<Button type="primary" loading={submitting} onClick={submitLaunch}>创建广告</Button>}>
        <Form form={launchForm} layout="vertical" className="website-ads-launch-form">
          <div className="website-ads-form-section"><h3>命名</h3><div className="website-ads-form-grid website-ads-form-grid--single"><Form.Item name="campaign_name" label="广告系列名称" rules={[{ required: true }]}><Input /></Form.Item><Form.Item name="adgroup_name" label="广告组名称" rules={[{ required: true }]}><Input /></Form.Item><Form.Item name="ad_name" label="广告名称" rules={[{ required: true }]}><Input /></Form.Item></div></div>
          <div className="website-ads-form-section">
            <div className="website-ads-form-section-head"><h3>推广商品与落地页</h3></div>
            <Form.Item name="landing_page_id" label="推广商品" rules={[{ required: true, message: '请选择推广商品' }]}>
              <Select
                showSearch
                optionFilterProp="label"
                options={landingPages.map((page) => ({ value: page.id, label: page.title }))}
                onChange={(value) => setSelectedPage(landingPages.find((page) => page.id === value))}
                placeholder="选择从 Magento 同步的商品"
              />
            </Form.Item>
            {selectedPage && <div className="website-ads-selected-product">
              {selectedPage.image_url ? <img src={selectedPage.image_url} alt="" /> : <div className="website-ads-mini-placeholder">无图</div>}
              <div><strong>{selectedPage.title}</strong><span>{selectedPage.landing_url}</span><span>{selectedPage.reference_price != null ? `${selectedPage.currency} ${Number(selectedPage.reference_price).toFixed(2)}` : '未设置参考价'}</span></div>
            </div>}
          </div>
          <div className="website-ads-form-section">
            <div className="website-ads-form-section-head"><div><h3>转化与素材</h3><span className="website-ads-section-note">TikTok 素材库共 {videoOptions.length} 个视频</span></div><Button type="primary" ghost onClick={() => { setUploadMode('file'); setUploadOpen(true); }}>上传素材</Button></div>
            <div className="website-ads-form-grid"><Form.Item name="pixel_id" label="Pixel" rules={[{ required: true, message: '请选择接收 View Content 事件的 Pixel' }]} extra="优化事件固定为 View Content，需由独立站服务器持续回传。"><Select options={pixelOptions} showSearch optionFilterProp="label" placeholder="选择 Pixel" /></Form.Item><Form.Item name="identity_id" label="TikTok 投放身份" rules={[{ required: true, message: '请选择投放身份' }]}><Select options={identityOptions} showSearch optionFilterProp="label" placeholder="选择已授权账号" /></Form.Item></div>
            <Form.Item name="video_id" label="视频素材" rules={[{ required: true, message: '请选择视频素材' }]}><Select options={videoOptions} showSearch optionFilterProp="label" placeholder="按名称搜索 TikTok 素材库视频" /></Form.Item>
            {selectedVideo && <div className="website-ads-video-preview">
              {selectedVideo.preview_url ? <video src={selectedVideo.preview_url} poster={selectedVideo.cover_url} controls preload="metadata" /> : selectedVideo.cover_url ? <img src={selectedVideo.cover_url} alt="视频封面" /> : <div className="website-ads-video-placeholder">该素材暂无预览地址</div>}
              <div className="website-ads-video-info"><strong>{selectedVideo.label}</strong><span>{selectedVideo.duration ? `${Number(selectedVideo.duration).toFixed(1)} 秒` : '时长待同步'}{selectedVideo.width && selectedVideo.height ? ` · ${selectedVideo.width}×${selectedVideo.height}` : ''}</span><span>ID {shortId(selectedVideo.value)}</span></div>
            </div>}
            <div className="website-ads-form-grid"><Form.Item name="call_to_action" label="行动按钮"><Select options={[['SHOP_NOW', '立即购买'], ['LEARN_MORE', '了解更多'], ['ORDER_NOW', '立即下单']].map(([value, label]) => ({ value, label }))} /></Form.Item></div>
            <Form.Item name="ad_text" label="广告文案" rules={[{ required: true, message: '请输入广告文案' }]}><Input.TextArea rows={3} showCount maxLength={100} /></Form.Item>
          </div>
          <div className="website-ads-form-section"><h3>预算与出价</h3><div className="website-ads-form-grid"><Form.Item name="daily_budget" label="每日预算" rules={[{ required: true }]}><InputNumber className="website-ads-full" min={1} precision={2} addonBefore="$" /></Form.Item><Form.Item name="bid_strategy" label="出价策略"><Segmented block options={[{ value: 'LOWEST_COST', label: '最低成本' }, { value: 'COST_CAP', label: '成本上限' }]} /></Form.Item>{bidStrategy === 'COST_CAP' && <Form.Item name="conversion_bid_price" label="目标 View Content 成本" rules={[{ required: true }]}><InputNumber className="website-ads-full" min={0.01} precision={2} addonBefore="$" /></Form.Item>}</div></div>
          <div className="website-ads-form-section"><h3>受众定向</h3><Form.Item name="location_ids" label="国家或地区" extra="默认投放美国；输入国家、州或城市名称可搜索更多地区。" rules={[{ required: true, message: '至少选择一个国家或地区' }]}><Select mode="multiple" filterOption={false} onSearch={findLocations} options={locationOptions} placeholder="输入国家、州或城市搜索" notFoundContent="请输入至少 2 个字符搜索" /></Form.Item><div className="website-ads-form-grid"><Form.Item name="gender" label="性别"><Radio.Group options={[{ value: 'GENDER_UNLIMITED', label: '不限' }, { value: 'GENDER_FEMALE', label: '女性' }, { value: 'GENDER_MALE', label: '男性' }]} /></Form.Item><Form.Item name="age_groups" label="年龄"><Checkbox.Group options={AGE_OPTIONS} /></Form.Item></div><Form.Item name="interest_category_ids" label="兴趣与购买意向" extra="必须选择经 TikTok 验证的兴趣或购买意向，系统不会创建广泛定向组。" rules={[{ required: true, message: '至少选择一个兴趣或购买意向' }]}><Select mode="multiple" filterOption={false} onSearch={findInterests} options={interestOptions} placeholder="例如：beauty、massage、sleep" notFoundContent="请输入至少 2 个字符搜索" /></Form.Item><div className="website-ads-form-grid"><Form.Item name="audience_ids" label="包含人群 ID（可选）"><Select mode="tags" tokenSeparators={[',', ' ']} /></Form.Item><Form.Item name="excluded_audience_ids" label="排除人群 ID（可选）"><Select mode="tags" tokenSeparators={[',', ' ']} /></Form.Item></div></div>
          <div className="website-ads-form-section"><div className="website-ads-form-section-head"><h3>Hermes 广告级点击守护</h3><Form.Item name="guard_enabled" valuePropName="checked" noStyle><Switch checkedChildren="开启" unCheckedChildren="关闭" /></Form.Item></div>{guardEnabled && <><div className="website-ads-form-grid"><Form.Item name="min_ctr_percent" label="CTR 最低值"><InputNumber className="website-ads-full" min={0.1} max={100} precision={1} addonAfter="%" /></Form.Item><Form.Item name="max_cpc" label="CPC 最高值"><InputNumber className="website-ads-full" min={0.01} precision={2} addonBefore="$" /></Form.Item><Form.Item name="min_impressions_before_action" label="CTR 最少曝光样本"><InputNumber className="website-ads-full" min={20} /></Form.Item><Form.Item name="min_clicks_for_cpc" label="CPC 最少点击样本"><InputNumber className="website-ads-full" min={1} /></Form.Item><Form.Item name="min_spend_before_action" label="最低判断消耗"><InputNumber className="website-ads-full" min={0.01} precision={2} addonBefore="$" /></Form.Item><Form.Item name="max_unprofitable_spend" label="零点击紧急上限（留空自动计算）"><InputNumber className="website-ads-full" min={0.01} precision={2} addonBefore="$" /></Form.Item></div><Alert type="info" showIcon message="没有固定观察时长" description="开始消耗并达到曝光或点击样本后立即判断；默认要求 CTR ≥ 4%、CPC ≤ $0.30，只暂停不达标的单条广告。" /></>}</div>
          <Alert type="info" showIcon message="安全创建" description="系统会自动附加 campaign、adgroup、ad、creative、placement 和 UTM 参数。建议先以暂停状态创建，检查无误后再开启。" />
          <Form.Item name="activate_after_create" label="创建后立即投放" valuePropName="checked"><Switch /></Form.Item>
        </Form>
      </Drawer>

      <Modal title="调整预算与出价" open={deliveryOpen} onCancel={() => setDeliveryOpen(false)} onOk={submitDelivery} confirmLoading={submitting} okText="保存并同步 TikTok"><Form form={deliveryForm} layout="vertical"><Form.Item name="daily_budget" label="每日预算"><InputNumber className="website-ads-full" min={1} precision={2} addonBefore="$" /></Form.Item><Form.Item name="conversion_bid_price" label="目标 View Content 成本"><InputNumber className="website-ads-full" min={0.01} precision={2} addonBefore="$" /></Form.Item><Alert type="warning" showIcon message="修改会立即作用于 TikTok 广告组，请确认当前投放阶段允许调整。" /></Form></Modal>
      <Modal title="批量上传视频素材" open={uploadOpen} onCancel={() => { setUploadOpen(false); setUploadFiles([]); setUploadProgress(null); uploadForm.resetFields(); }} onOk={submitUpload} confirmLoading={submitting} okText={uploadMode === 'file' ? `上传 ${uploadFiles.length || ''}` : '上传到 TikTok'} width={620}>
        <Form form={uploadForm} layout="vertical">
          {selectedPage && <div className="website-ads-upload-product"><span>关联推广商品</span><strong>{selectedPage.title}</strong></div>}
          <Segmented block value={uploadMode} onChange={setUploadMode} options={[{ value: 'file', label: '本地文件' }, { value: 'url', label: '视频 URL' }]} />
          {uploadMode === 'file' ? <Form.Item label="选择视频" required>
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
          </Form.Item> : <Form.Item name="video_url" label="视频 URL" rules={[{ required: true, type: 'url' }]}><Input placeholder="https://cdn.example.com/video.mp4" /></Form.Item>}
          {uploadMode === 'url' ? <Form.Item name="file_name" label="素材名称" rules={[{ required: true }]}><Input maxLength={100} placeholder="product-demo-01.mp4" /></Form.Item> : null}
          <Alert type="info" showIcon message={uploadMode === 'file' ? '系统会按 TikTok 官方协议逐个可靠上传，并自动提交 Hermes 内容分析。' : 'URL 必须允许 TikTok 从公网直接下载，不能依赖登录、Cookie 或临时防盗链。'} description={uploadMode === 'file' ? '无需手工填写描述：Hermes 会识别商品、生成视频描述并区分真实达人、真实用户与 AIGC。' : undefined} />
          {uploadProgress ? <Alert type={uploadProgress.failed ? 'warning' : 'info'} showIcon message={`处理进度 ${uploadProgress.completed}/${uploadProgress.total}`} description={`新增与重复文件会自动区分${uploadProgress.duplicates ? `，已忽略重复 ${uploadProgress.duplicates} 个` : ''}${uploadProgress.failed ? `，失败 ${uploadProgress.failed} 个` : ''}。`} /> : null}
        </Form>
      </Modal>
      <Modal title="连接 Magento" open={connectionOpen} onCancel={() => setConnectionOpen(false)} onOk={submitConnection} confirmLoading={submitting} okText="连接并同步"><Form form={connectionForm} layout="vertical"><Form.Item name="name" label="连接名称" rules={[{ required: true }]}><Input placeholder="MYUPONA Magento" /></Form.Item><Form.Item name="base_url" label="Magento 地址" rules={[{ required: true, type: 'url' }]}><Input placeholder="https://www.example.com" /></Form.Item><Form.Item name="access_token" label="Integration Token" rules={[{ required: true }]}><Input.Password /></Form.Item></Form></Modal>
      <Modal title="手工添加落地页" open={manualOpen} onCancel={() => setManualOpen(false)} onOk={submitManualPage} confirmLoading={submitting} okText="添加"><Form form={manualForm} layout="vertical"><Form.Item name="title" label="名称" rules={[{ required: true }]}><Input /></Form.Item><Form.Item name="landing_url" label="HTTPS 落地页" rules={[{ required: true, type: 'url' }]}><Input /></Form.Item><div className="website-ads-form-grid"><Form.Item name="reference_price" label="参考成交价"><InputNumber className="website-ads-full" min={0} precision={2} addonBefore="$" /></Form.Item><Form.Item name="image_url" label="缩略图 URL"><Input /></Form.Item></div></Form></Modal>
    </main>
  );
}
