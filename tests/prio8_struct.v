// Revision: one-hot "highest set bit" mask feeding an OR-plane encoder.
module prio8(input [7:0] r, output [2:0] idx, output vld);
  wire [7:0] m;

  assign m[7] = r[7];
  assign m[6] = r[6] & ~r[7];
  assign m[5] = r[5] & ~(r[7] | r[6]);
  assign m[4] = r[4] & ~(r[7] | r[6] | r[5]);
  assign m[3] = r[3] & ~(r[7] | r[6] | r[5] | r[4]);
  assign m[2] = r[2] & ~(r[7] | r[6] | r[5] | r[4] | r[3]);
  assign m[1] = r[1] & ~(r[7] | r[6] | r[5] | r[4] | r[3] | r[2]);
  assign m[0] = r[0] & ~(r[7] | r[6] | r[5] | r[4] | r[3] | r[2] | r[1]);

  assign idx[2] = m[7] | m[6] | m[5] | m[4];
  assign idx[1] = m[7] | m[6] | m[3] | m[2];
  assign idx[0] = m[7] | m[5] | m[3] | m[1];
  assign vld    = |r;
endmodule
