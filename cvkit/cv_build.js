#!/usr/bin/env node
/* cvkit — build a tailored one-page CV or a supporting statement from a JSON config.
   Usage:  node cv_build.js bases/<config>.json out.docx
   Requires: npm install docx
   The Notion Block Library is authoritative for wording. blocks.json is its build artefact. */
const {Document,Packer,Paragraph,TextRun,ExternalHyperlink,AlignmentType,BorderStyle,LevelFormat,convertInchesToTwip}=require('docx');
const fs=require('fs'), path=require('path');

const B=JSON.parse(fs.readFileSync(path.join(__dirname,'blocks.json'),'utf8'));
const ID=B.identity;

const link=(t,u,sz)=>new ExternalHyperlink({link:u,children:[new TextRun({text:t,size:sz||18,font:"Calibri",color:"1155CC",underline:{}})]});
const contactRuns=()=>[
  new TextRun({text:ID.location+"  |  "+ID.phone+"  |  "+ID.email+"  |  ",size:18,font:"Calibri",color:"444444"}),
  link("Portfolio",ID.portfolio), new TextRun({text:"  |  ",size:18,font:"Calibri",color:"444444"}), link("LinkedIn",ID.linkedin),
];
const H=t=>new Paragraph({spacing:{before:115,after:55},border:{bottom:{style:BorderStyle.SINGLE,size:6,color:"888888",space:2}},children:[new TextRun({text:t,bold:true,size:19,font:"Calibri",color:"222222"})]});
const BUL=runs=>new Paragraph({numbering:{reference:"b",level:0},spacing:{after:42},children:runs.map(r=>new TextRun({text:r.t,bold:r.b,size:19,font:"Calibri"}))});
const NUM={config:[{reference:"b",levels:[{level:0,format:LevelFormat.BULLET,text:"\u2022",alignment:AlignmentType.LEFT,style:{paragraph:{indent:{left:convertInchesToTwip(0.22),hanging:convertInchesToTwip(0.15)}}}}]}]};

function resolve(v){ // "H3" or "PRJ-SORA" -> approved text; plain string passes through
  for(const g of ['headlines','profiles','projects','experience','statement']) if(B[g]&&B[g][v]!==undefined) return B[g][v];
  return v;
}

function buildCV(cfg,out){
  const kids=[
    new Paragraph({spacing:{after:20},children:[new TextRun({text:ID.name,bold:true,size:30,font:"Calibri"})]}),
    new Paragraph({spacing:{after:120},children:contactRuns()}),
    new Paragraph({spacing:{after:40},children:[new TextRun({text:cfg.title,bold:true,size:22,font:"Calibri"})]}),
    new Paragraph({spacing:{after:100},children:[new TextRun({text:resolve(cfg.profile),size:19,font:"Calibri"})]}),
    ...cfg.headlines.map(h=>BUL([{t:resolve(h),b:true}])),
  ];
  cfg.sections.forEach(s=>{kids.push(H(s.h)); s.bullets.forEach(b=>{
    kids.push(typeof b==='string'?BUL([{t:resolve(b)}]):BUL(b));
  });});
  const doc=new Document({numbering:NUM,sections:[{properties:{page:{margin:{top:520,bottom:440,left:660,right:660}}},children:kids}]});
  return Packer.toBuffer(doc).then(b=>fs.writeFileSync(out,b));
}

function buildStatement(cfg,out){
  const kids=[
    new Paragraph({spacing:{after:20},children:[new TextRun({text:ID.name,bold:true,size:26,font:"Calibri"})]}),
    new Paragraph({spacing:{after:200},border:{bottom:{style:BorderStyle.SINGLE,size:6,color:"888888",space:3}},children:contactRuns()}),
    new Paragraph({spacing:{after:160},children:[new TextRun({text:cfg.title,bold:true,size:22,font:"Calibri"})]}),
  ];
  cfg.paragraphs.forEach(p=>{
    if(p.heading) kids.push(new Paragraph({spacing:{before:130,after:50,line:280},children:[new TextRun({text:p.heading,bold:true,size:21,font:"Calibri"})]}));
    if(p.close){
      kids.push(new Paragraph({spacing:{after:140,line:280},children:[
        new TextRun({text:"I would be genuinely glad of the chance to be considered. My portfolio is ",size:21,font:"Calibri"}),
        link("here",ID.portfolio,21), new TextRun({text:".",size:21,font:"Calibri"})]}));
    } else if(p.text){
      kids.push(new Paragraph({spacing:{after:140,line:280},children:[new TextRun({text:resolve(p.text),size:21,font:"Calibri"})]}));
    }
  });
  const doc=new Document({sections:[{properties:{page:{margin:{top:900,bottom:900,left:1000,right:1000}}},children:kids}]});
  return Packer.toBuffer(doc).then(b=>fs.writeFileSync(out,b));
}

function selfCheck(cfg){
  const problems=[]; const flat=JSON.stringify(cfg);
  if(cfg.kind==='statement'){
    if(/[\u2014\u2013]/.test(flat)) problems.push('em/en dash present in a statement (Profile §6.3 forbids)');
    if(/\d\s?%/.test(flat)) problems.push('percentage written with the % sign; use "60 to 70 per cent"');
  }
  ['organization','recognize','analyze','center','behavior'].forEach(w=>{ if(new RegExp(w,'i').test(flat)) problems.push('US spelling: '+w); });
  [/passionate/i,/leverage/i,/seamless/i,/synergy/i,/aligns with my values/i].forEach(r=>{ if(r.test(flat)) problems.push('hollow word matched '+r); });
  if(cfg.kind==='cv' && (!cfg.headlines||cfg.headlines.length<4)) problems.push('CV needs 4 headline numbers in the top third');
  if(!cfg.title) problems.push('missing title line (must be the advert job title verbatim)');
  return problems;
}

const cfgPath=process.argv[2], out=process.argv[3];
if(!cfgPath||!out){ console.error('usage: node cv_build.js bases/<config>.json <out.docx>'); process.exit(1); }
const cfg=JSON.parse(fs.readFileSync(cfgPath,'utf8'));
const problems=selfCheck(cfg);
(cfg.kind==='statement'?buildStatement:buildCV)(cfg,out).then(()=>{
  if(problems.length){ console.log('PROBLEMS:'); problems.forEach(p=>console.log('  - '+p)); }
  else console.log('clean');
  console.log('wrote '+out);
  console.log('NEXT: render to PDF and confirm it is ONE page:');
  console.log('  soffice --headless --convert-to pdf '+out);
});
