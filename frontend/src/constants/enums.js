export const Role={admin:'业务负责人',analyst:'分析员',viewer:'观察员'};
export const JobStatus={queued:'排队中',running:'计算中',completed:'已完成',failed:'执行失败'};
export const DeliveryStatus={
 queued:'排队等待空间',
 writing:'写入中',
 stored:'收件箱可见',
 retained:'已保留',
 reclaiming:'回收中',
 reclaimed:'已回收',
 rejected:'已拒绝',
};
export const ReclaimReason={eviction:'容量不足自动回收',rejected:'超上限拒绝',manual:'人工处置'};
export function formatBytes(bytes){
 const n=Number(bytes)||0;
 if(n<1024)return n+' B';
 const units=['KB','MB','GB','TB'];let v=n,i=-1;
 do{v/=1024;i++;}while(v>=1024&&i<units.length-1);
 return v.toFixed(v<10?2:1)+' '+units[i];
}
