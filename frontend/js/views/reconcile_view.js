export const reconcileViewTemplate = `
<section class="panel">
  <h2>仓间对账互证</h2>
  <p class="hint">甲路径：封批快照校验和与温谱摘要须同源；不一致则正式温谱册零行。</p>
  <div class="form-row">
    <div><label>仓间编码</label><input v-model="reconCode"/></div>
    <div><label>批次互证编码</label><input v-model="proofCode"/></div>
  </div>
  <div class="actions">
    <button @click="loadRecon">对账单仓</button>
    <button class="secondary" @click="loadProof">批次互证</button>
    <button class="secondary" @click="loadReconAll">全仓对账</button>
    <button class="secondary" @click="loadSameSource">同源总览</button>
  </div>
  <div v-if="reconOne" class="detail-box">
    <p>配额 {{ reconOne.quota_batches }} · 在批 {{ reconOne.active_batches }} · <span :class="reconOne.balanced ? 'ok' : 'bad'">{{ reconOne.balanced ? "平衡" : "失衡" }}</span></p>
  </div>
  <div v-if="proofOne" class="detail-box">
    <p>状态 {{ proofOne.status }} · 开放游标 {{ proofOne.open_excursions }} · 温谱 {{ proofOne.spectrum_exports }}</p>
    <p>封批校验和 <span class="mono">{{ proofOne.same_source && proofOne.same_source.seal_checksum || "—" }}</span> ·
       温谱校验和 <span class="mono">{{ proofOne.same_source && proofOne.same_source.export_checksum || "—" }}</span> ·
       <span class="badge" :class="proofOne.spectrum_matches_seal ? 'pass' : 'fail'">{{ proofOne.spectrum_matches_seal ? "同源" : "同源偏航" }}</span></p>
  </div>
  <div v-if="sameSourceSummary" class="detail-box">
    <p>同源总览：共 {{ sameSourceSummary.total }} 批 · 同源 {{ sameSourceSummary.same_source }} · 偏航 {{ sameSourceSummary.drifted }}</p>
    <p v-if="sameSourceSummary.drifted" class="bad">偏航批次：{{ sameSourceSummary.drifted_batches.join("、") }}</p>
  </div>
  <table class="data-table">
    <thead><tr><th>仓间</th><th>区域</th><th>在批</th><th>余配额</th><th>平衡</th></tr></thead>
    <tbody>
      <tr v-for="r in reconAllItems" :key="r.chamber_code">
        <td>{{ r.chamber_code }}</td>
        <td>{{ r.region }}</td>
        <td>{{ r.active_batches }}</td>
        <td>{{ r.quota_left }}</td>
        <td>{{ r.balanced ? "是" : "否" }}</td>
      </tr>
    </tbody>
  </table>
</section>
`;
