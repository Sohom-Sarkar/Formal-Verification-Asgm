// Revision: parameterised carry-select adder in 4-bit blocks.
// Each block speculatively computes its sum for both possible incoming
// carries and selects between them once the real carry arrives, so the carry
// path is a chain of multiplexers rather than a chain of full adders.
module addN #(parameter WIDTH = 16)
             (input [WIDTH-1:0] a, input [WIDTH-1:0] b, input cin,
              output [WIDTH-1:0] sum, output cout);
  localparam NB = WIDTH / 4;

  wire [5*NB-1:0] s0;          // block sum assuming carry-in 0
  wire [5*NB-1:0] s1;          // block sum assuming carry-in 1
  wire [NB:0] c;

  assign c[0] = cin;

  genvar j;
  generate
    for (j = 0; j < NB; j = j + 1) begin : blk
      assign s0[5*j+4 : 5*j] = {1'b0, a[4*j+3 : 4*j]} + {1'b0, b[4*j+3 : 4*j]};
      assign s1[5*j+4 : 5*j] = {1'b0, a[4*j+3 : 4*j]} + {1'b0, b[4*j+3 : 4*j]}
                             + 5'd1;
      assign sum[4*j+3 : 4*j] = c[j] ? s1[5*j+3 : 5*j] : s0[5*j+3 : 5*j];
      assign c[j+1]           = c[j] ? s1[5*j+4]       : s0[5*j+4];
    end
  endgenerate

  assign cout = c[NB];
endmodule
