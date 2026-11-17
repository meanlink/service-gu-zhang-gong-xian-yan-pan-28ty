/** @typedef {{id:number,name:string,source:string,rows:number,checksum:string}} Dataset */
/** @typedef {{id:number,name:string,dataset_id:number,status:string,result:Object,approved:boolean}} AnalysisJob */
/** @typedef {{id:number,name:string,job_id:number,destination:string,next_run:number,enabled:boolean}} ReportSchedule */
/**
 * @typedef {Object} DeliveryArchive
 * @property {number} id
 * @property {string} status queued|writing|stored|retained|reclaiming|reclaimed|rejected
 * @property {boolean} visible
 * @property {boolean} retained
 * @property {number} size_bytes 两份附件实际字节合计
 * @property {string[]} written_kinds
 * @property {string[]} attachment_kinds
 * @property {string} note 排队或拒绝原因
 * @property {number} attempts
 */
/**
 * @typedef {Object} StorageStats
 * @property {number} capacity_limit_bytes
 * @property {number} used_bytes
 * @property {number} available_bytes
 * @property {boolean} over_limit
 * @property {number} attachment_count
 * @property {Object<string,number>} deliveries
 */
export {};
