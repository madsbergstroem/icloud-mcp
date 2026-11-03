/*!
 * (C) Ionic http://ionicframework.com - MIT License
 */
import{e as o,w as s}from"./p-BpRUsN_W.js";import{a as t,s as r}from"./p-BWKtcudz.js";import{c as a}from"./p-B2y1T0dN.js";import"./p-Dj4J3LkD.js";const p=()=>{const p=window;p.addEventListener("statusTap",(()=>{o((()=>{const o=document.elementFromPoint(p.innerWidth/2,p.innerHeight/2);if(!o)return;const n=t(o);n&&new Promise((o=>a(n,o))).then((()=>{s((async()=>{n.style.setProperty("--overflow","hidden"),await r(n,300),n.style.removeProperty("--overflow")}))}))}))}))};export{p as startStatusTap}