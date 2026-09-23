import { chromium } from "playwright";
import fs from "node:fs";

const targets = [
  {name:"iphone-se", width:320, height:568},
  {name:"iphone-8", width:375, height:667},
  {name:"iphone-16e", width:390, height:844},
  {name:"iphone-pro-max", width:430, height:932},
  {name:"mobile-landscape", width:844, height:390},
];

fs.mkdirSync("artifacts/mobile", {recursive:true});
const browser = await chromium.launch({headless:true});
let failures = [];

for (const target of targets) {
  const page = await browser.newPage({viewport:{width:target.width,height:target.height}, deviceScaleFactor:1});
  page.on("console", msg => {
    if (msg.type() === "error") failures.push(target.name + ": console error: " + msg.text());
  });
  await page.goto("http://127.0.0.1:4173/history/sangokushi-globe/", {waitUntil:"networkidle"});
  await page.screenshot({path:"artifacts/mobile/" + target.name + ".png", fullPage:true});

  const result = await page.evaluate(() => {
    const rect = sel => {
      const r = document.querySelector(sel).getBoundingClientRect();
      return {left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:r.width,height:r.height,cx:r.left+r.width/2,cy:r.top+r.height/2};
    };
    const stage=rect(".stage"), timeline=rect(".timelineCard"), story=rect(".story");
    const play=rect("#playBtn"), prev=rect("#prevBtn"), slider=rect("#yearSlider"), next=rect("#nextBtn"), romance=rect("#romBtn");
    const app=rect(".app");
    return {
      viewport:{w:innerWidth,h:innerHeight},
      scrollWidth:document.documentElement.scrollWidth,
      bodyScrollWidth:document.body.scrollWidth,
      stage,timeline,story,play,prev,slider,next,romance,app,
      eventCount:document.querySelectorAll(".eventChip").length,
      sliderMax:Number(document.querySelector("#yearSlider").max),
      sceneNo:document.querySelector("#sceneNo")?.textContent || "",
      titleText:document.querySelector("#title")?.textContent || "",
      yearText:document.querySelector("#yearText")?.textContent || "",
      loadingPresent:!!document.querySelector("#loading"),
    };
  });

  const err = msg => failures.push(target.name + ": " + msg);
  if (result.scrollWidth > target.width + 1 || result.bodyScrollWidth > target.width + 1) err("horizontal overflow");
  for (const [name,r] of Object.entries({stage:result.stage,timeline:result.timeline,story:result.story,app:result.app})) {
    if (r.left < -1 || r.right > target.width + 1) err(name + " outside viewport: " + JSON.stringify(r));
  }
  if (result.timeline.top < result.stage.bottom - 2) err("timeline overlaps globe");
  if (result.story.top < result.timeline.bottom - 2) err("story overlaps timeline");
  if (result.stage.height < 220) err("globe stage too short: " + result.stage.height);
  if (result.eventCount < 40) err("story is too coarse; expected at least 40 scenes, got " + result.eventCount);
  if (result.sliderMax !== result.eventCount - 1) err("scene slider max mismatch: " + result.sliderMax + " vs " + (result.eventCount - 1));
  if (!/^SCENE 01 \/ \d{2}$/.test(result.sceneNo)) err("scene counter missing or malformed: " + result.sceneNo);

  if (target.name === "iphone-se") {
    const before = {title:result.titleText, year:result.yearText};
    await page.click("#nextBtn");
    const after = await page.evaluate(() => ({
      title:document.querySelector("#title")?.textContent || "",
      year:document.querySelector("#yearText")?.textContent || "",
      scene:document.querySelector("#sceneNo")?.textContent || ""
    }));
    if (before.year !== "184" || after.year !== "184") err("same-year scene navigation skipped 184 sequence: " + JSON.stringify({before,after}));
    if (before.title === after.title) err("next scene did not change title");
    if (!after.scene.startsWith("SCENE 02 /")) err("next scene counter incorrect: " + after.scene);
  }

  if (target.width <= 430 && target.height > target.width) {
    const rowY = [result.play.cy,result.prev.cy,result.slider.cy,result.next.cy];
    if (Math.max(...rowY)-Math.min(...rowY) > 8) err("primary timeline controls are split across rows: " + rowY.join(","));
    if (result.romance.cy <= Math.max(...rowY)+16) err("romance toggle should be a separate row");
    for (const [name,r] of Object.entries({play:result.play,prev:result.prev,next:result.next,romance:result.romance})) {
      if (r.height < 40) err(name + " tap target too short: " + r.height);
    }
  }
  await page.close();
}
await browser.close();

if (failures.length) {
  console.error("\nMOBILE LAYOUT FAILURES\n" + failures.map(x=>" - "+x).join("\n"));
  process.exit(1);
}
console.log("All mobile layout checks passed.");
