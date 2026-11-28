/** 跨模块会展引擎 API：执行历史、配额视图、进度推进等。 */
import { request } from './client'
import type { ExhibitionHistory, QuotaSummary, DAGStatus, HandoverResult } from '@/data/exhibition-types'

const EXHIBITION_BASE = '/api/exhibition'

export async function fetchBoothHistory(boothNo: string): Promise<ExhibitionHistory> {
  const response = await request(`${EXHIBITION_BASE}/${boothNo}/history`)
  if (!response.ok) throw new Error('执行历史查询失败')
  return await response.json()
}

export async function advanceConstructionNode(nodeId: number): Promise<{ ok: boolean; reason: string }> {
  const response = await request(`${EXHIBITION_BASE}/advance/${nodeId}`, { method: 'POST' })
  return await response.json()
}

export async function fetchQuotaSummary(): Promise<QuotaSummary> {
  const response = await request(`${EXHIBITION_BASE}/quota`)
  if (!response.ok) throw new Error('配额数据读取失败')
  return await response.json()
}

export async function signoffChecklist(checklistId: number): Promise<{ ok: boolean; reason: string }> {
  const response = await request(`${EXHIBITION_BASE}/checklist/${checklistId}/signoff`, { method: 'POST' })
  return await response.json()
}

export async function confirmHandover(handoverId: number): Promise<HandoverResult> {
  const response = await request(`${EXHIBITION_BASE}/handover/${handoverId}/confirm`, { method: 'POST' })
  return await response.json()
}

export interface ChangeIssueResult {
  ok: boolean
  reason: string
  receipt_no?: string
  receipt_kind?: string
  affected_nodes?: { id: number; name: string }[]
  affected_checklists?: { id: number; item: string }[]
  first_issuance?: ChangeFirstIssuance | null
}

export interface ChangeFirstIssuance {
  receipt_no: string
  change_id: number
  complete: boolean
  affected_booths: string[]
  affected_nodes: { id: number; name: string }[]
  affected_checklists: { id: number; item: string }[]
  reverted_nodes: number
  reverted_checklists: number
}

export async function issueChangeOrder(
  changeId: number,
  receiptNo?: string,
): Promise<ChangeIssueResult> {
  const query = receiptNo ? `?receipt_no=${encodeURIComponent(receiptNo)}` : ''
  const response = await request(`${EXHIBITION_BASE}/change/${changeId}/issue${query}`, {
    method: 'POST',
  })
  return await response.json()
}

export async function fetchChangeReceipt(
  changeId: number,
  receiptNo?: string,
): Promise<{ ok: boolean; receipt_no?: string; first_issuance?: ChangeFirstIssuance; reason?: string }> {
  const query = receiptNo ? `?receipt_no=${encodeURIComponent(receiptNo)}` : ''
  const response = await request(`${EXHIBITION_BASE}/change/${changeId}/receipt${query}`)
  return await response.json()
}

export async function fetchSafetyStatus(boothNo: string): Promise<{ ok: boolean; reason: string }> {
  const response = await request(`${EXHIBITION_BASE}/safety/${boothNo}`)
  return await response.json()
}

export async function fetchDAGStatus(): Promise<DAGStatus> {
  const response = await request(`${EXHIBITION_BASE}/dag/validate`)
  return await response.json()
}

export async function fetchScheduleConflicts(): Promise<{ conflicts: Record<string, unknown>[]; count: number }> {
  const response = await request(`${EXHIBITION_BASE}/conflicts`)
  return await response.json()
}