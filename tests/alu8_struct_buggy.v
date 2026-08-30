// Same structural ALU, but the two shift operands are swapped in the mux tree,
// so op=5 and op=6 return each other's result.
module alu8(input [7:0] a, input [7:0] b, input [2:0] op,
            output [7:0] y, output zero);
  wire [7:0] sum, diff, and_r, or_r, xor_r, shl, shr, lt_r;
  wire [8:0] ext;

  assign sum   = a + b;
  assign diff  = a + (~b) + 8'd1;
  assign and_r = a & b;
  assign or_r  = a | b;
  assign xor_r = a ^ b;
  assign shl   = a << b[2:0];
  assign shr   = a >> b[2:0];

  assign ext   = {1'b0, a} + {1'b0, ~b} + 9'd1;
  assign lt_r  = {7'd0, ~ext[8]};

  wire [7:0] l0, l1, l2, l3, h0, h1;
  assign l0 = op[0] ? diff  : sum;
  assign l1 = op[0] ? or_r  : and_r;
  assign l2 = op[0] ? shr   : xor_r;    // BUG: should be shl
  assign l3 = op[0] ? lt_r  : shl;      // BUG: should be shr
  assign h0 = op[1] ? l1 : l0;
  assign h1 = op[1] ? l3 : l2;
  assign y  = op[2] ? h1 : h0;

  assign zero = ~(|y);
endmodule
