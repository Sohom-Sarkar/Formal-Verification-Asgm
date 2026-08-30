// 16-bit ripple-carry adder with EXACTLY ONE broken gate.
//
// The carry out of bit 9 should be  (a9 & b9) | (c9 & (a9 ^ b9))  but the
// propagate term is missing, so a carry arriving at bit 9 is swallowed
// instead of being passed on. A single-gate fault, which is what makes this
// the right test case for single-fix fault localisation.
module adder16(input [15:0] a, input [15:0] b, input cin,
               output [15:0] sum, output cout);
  wire [16:0] c;
  assign c[0] = cin;

  genvar i;
  generate
    for (i = 0; i < 16; i = i + 1) begin : stage
      assign sum[i] = a[i] ^ b[i] ^ c[i];
      if (i == 9) begin
        assign c[i+1] = (a[i] & b[i]);                        // BUG
      end else begin
        assign c[i+1] = (a[i] & b[i]) | (c[i] & (a[i] ^ b[i]));
      end
    end
  endgenerate

  assign cout = c[16];
endmodule
