import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';

const source=readFileSync(new URL('./page.html',import.meta.url),'utf8');
const context=vm.createContext({});
vm.runInContext(source.split('// ROW_HEIGHTS_START')[1].split('// ROW_HEIGHTS_END')[0]+';globalThis.RowHeights=RowHeights;',context);
const {RowHeights}=context;

test('empty, single-row and exact row-boundary lookups',()=>{
  const empty=new RowHeights(0,64);assert.equal(empty.total,0);assert.equal(empty.rowAt(100),0);
  const heights=new RowHeights(3,64);
  for(const [offset,index] of [[-1,0],[0,0],[63,0],[64,1],[127,1],[128,2],[192,2],[10000,2]])assert.equal(heights.rowAt(offset),index);
  heights.set(0,128);assert.equal(heights.prefix(1),128);assert.equal(heights.rowAt(128),1);assert.equal(heights.total,256);
});

test('variable row measurements preserve prefix sums and offset lookup across a full collection',()=>{
  const count=14733,heights=new RowHeights(count,64),expected=Array(count).fill(64);
  let seed=42;const random=limit=>{seed=(Math.imul(seed,1664525)+1013904223)>>>0;return seed%limit;};
  for(let update=0;update<1000;update++){
    const index=random(count),height=(1+random(64000))/64;
    heights.set(index,height);expected[index]=height;
  }
  let sum=0;
  expected.forEach((height,index)=>{
    assert.equal(heights.prefix(index),sum);
    assert.equal(heights.rowAt(sum),index);
    assert.equal(heights.rowAt(sum+height/2),index);
    sum+=height;
  });
  assert.equal(heights.total,sum);assert.equal(heights.rowAt(sum+1000),count-1);
});
